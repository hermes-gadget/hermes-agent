import { describe, expect, it } from 'vitest'

import { toChatMessages } from './hydration'

// A scheduled job's Bot Chat report (`cron_report`) persists as `control_turn`: the payload is the
// bot's input, never a row the person reads. Only the bot's own reply may appear in the transcript.
describe('toChatMessages with a cron control turn', () => {
  it('drops the report row and keeps the bot reply', () => {
    const messages = [
      {
        role: 'user',
        content: '[Scheduled task "warm-worker-measure" — automatic report, not a message from the user.]\n\nping',
        timestamp: 100,
        display_kind: 'control_turn'
      },
      { role: 'assistant', content: 'Disk on ukmesh-vps is at 91%; cleared 4G of logs.', timestamp: 101 }
    ]

    const result = toChatMessages(messages as never)

    expect(result.map(m => m.role)).toEqual(['assistant'])
    expect(result[0].content).toContain('91%')
  })

  it('leaves an ordinary user row alone', () => {
    const messages = [{ role: 'user', content: 'hello', timestamp: 200 }]

    expect(toChatMessages(messages as never).map(m => m.role)).toEqual(['user'])
  })
})
