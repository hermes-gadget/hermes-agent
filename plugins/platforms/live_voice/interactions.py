"""Durable foreground questions, separate from asynchronous job commentary."""
import asyncio
from dataclasses import dataclass
import hashlib
import json


@dataclass(frozen=True)
class PendingVoiceInteraction:
    prompt_id: str
    status: str
    question: str
    choices: list
    needs_restate: bool = False


class InteractionManager:
    def __init__(self, assistant):
        self.assistant = assistant
        self.cached = []

    async def records(self):
        a = self.assistant
        if a.db is None:
            return []
        scope, lineage = await asyncio.to_thread(a.conversation.identity)
        return await asyncio.to_thread(a.db.voice_records, "interaction", a.owner_key, scope=scope, session_id=lineage)

    async def snapshot(self):
        self.cached = [{"prompt_id": r["id"], "status": r["status"], **r["payload"]}
                for r in await self.records() if r["status"] in {"foreground", "queued"}]
        return self.cached

    async def intercept(self, text, did):
        foreground = [r for r in await self.snapshot() if r["status"] == "foreground"]
        if len(foreground) != 1:
            return None
        prompt = foreground[0]
        simple = text.casefold().strip(".! ") in {"yes", "yes please", "no", "no thanks", "do that", "go ahead", "proceed", "cancel"}
        choice = text.isdigit() or any(c.casefold() == text.casefold() for c in prompt["choices"])
        if not (simple or choice or prompt.get("native") and not prompt["choices"]):
            return None
        if prompt.get("needs_restate"):
            result = await self.prompt({"prompt_id": prompt["prompt_id"]}, did)
            return "Please ask this question again before acting: " + result["question"]
        result = await self.respond({"prompt_id": prompt["prompt_id"], "response": text}, did)
        return "Verified answer result: " + json.dumps(result, ensure_ascii=False)

    async def prompt(self, args, did, *, background=False):
        a = self.assistant
        if a.db is None:
            raise ValueError("Durable questions are unavailable.")
        existing = next((r for r in await self.records() if r["id"] == args.get("prompt_id")), None)
        if existing:
            data = {**existing["payload"], "needs_restate": False}
            row = await asyncio.to_thread(a.db.voice_change_record, "interaction", existing["id"], a.owner_key,
                {"foreground", "queued"}, existing["status"], data)
            if row is None:
                raise ValueError("This question is no longer pending.")
        else:
            question = str(args.get("question") or "").strip()
            choices = args.get("choices") or []
            action = args.get("proposed_action")
            if not question or len(question) > 2000 or not isinstance(choices, list) or len(choices) > 12 or any(
                    not isinstance(c, str) or not c.strip() or len(c) > 300 for c in choices):
                raise ValueError("Provide a concise question and at most twelve text choices.")
            if action is not None and (not isinstance(action, dict) or action.get("action") not in {
                    "dispatch", "continue", "steer", "stop", "select", "handoff", "notifications"}):
                raise ValueError("The proposed action must use an existing assistant operation.")
            scope, lineage = await asyncio.to_thread(a.conversation.identity)
            ident = hashlib.sha256(f"{a.owner_key}:{scope}:{lineage}:{did}".encode()).hexdigest()
            payload = {"question": question, "choices": choices, "proposed_action": action,
                       "native": args.get("native"), "needs_restate": False}
            row = await asyncio.to_thread(a.db.voice_create_record, "interaction", ident, a.owner_key,
                payload, scope=scope, session_id=lineage, status="queued")
        if not background:
            scope, lineage = await asyncio.to_thread(a.conversation.identity)
            if not await asyncio.to_thread(a.db.voice_focus_prompt, row["id"], a.owner_key, scope, lineage):
                return {"prompt_id": row["id"], "status": row["status"], "note": "Question already settled."}
            row["status"] = "foreground"
        await a.quiet("Pending question state (data): " + json.dumps(await self.snapshot()))
        return {"prompt_id": row["id"], "status": row["status"], **row["payload"]}

    async def interrupted(self):
        a = self.assistant
        for row in await self.records():
            if row["status"] == "foreground":
                await asyncio.to_thread(a.db.voice_change_record, "interaction", row["id"], a.owner_key,
                    {"foreground"}, "foreground", {**row["payload"], "needs_restate": True})

    async def respond(self, args, did):
        a = self.assistant
        records = await self.records()
        ident = args.get("prompt_id")
        matches = [r for r in records if r["id"] == ident] if ident else [r for r in records if r["status"] == "foreground"]
        if len(matches) != 1:
            raise ValueError("Choose the pending question explicitly.")
        row, answer = matches[0], str(args.get("response") or "").strip()
        if row["status"] not in {"foreground", "queued"}:
            return {"prompt_id": row["id"], "status": row["status"], "result": row["payload"].get("result")}
        data = row["payload"]
        if not answer or len(answer) > 6000:
            raise ValueError("A concise answer is required.")
        if data.get("needs_restate"):
            return {"prompt_id": row["id"], "status": "needs_restate", "question": data["question"],
                    "note": "Repeat this question using prompt before accepting an answer."}
        choices = data.get("choices") or []
        if choices:
            answer = next((c for c in choices if c.casefold() == answer.casefold()),
                          choices[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(choices) else "")
            if not answer:
                raise ValueError("Choose one of the recorded choices.")
        action = data.get("proposed_action")
        affirmative = answer.casefold().strip(".! ") in {"yes", "yes please", "do that", "go ahead", "proceed"}
        negative = answer.casefold().strip(".! ") in {"no", "no thanks", "cancel", "don't"}
        if action and not affirmative and not negative:
            raise ValueError("Confirm or decline the recorded action explicitly.")
        claimed = await asyncio.to_thread(a.db.voice_change_record, "interaction", row["id"], a.owner_key,
            {"foreground", "queued"}, "answering", {**data, "response": answer})
        if claimed is None:
            return {"prompt_id": row["id"], "status": "already_settling"}
        try:
            if data.get("native"):
                result = await a.jobs.answer_clarify(data["native"], answer)
            elif action and affirmative:
                result = await a.handle(action, "prompt:" + row["id"])
            else:
                result = {"response": answer}
            state = "cancelled" if action and negative else "answered"
        except Exception:
            await asyncio.to_thread(a.db.voice_change_record, "interaction", row["id"], a.owner_key,
                {"answering"}, "uncertain")
            raise
        await asyncio.to_thread(a.db.voice_change_record, "interaction", row["id"], a.owner_key,
            {"answering"}, state, {**data, "response": answer, "result": result})
        await a.quiet("Question settled: " + row["id"] + "; " + state)
        await self.snapshot()
        return {"prompt_id": row["id"], "status": state, "result": result}
