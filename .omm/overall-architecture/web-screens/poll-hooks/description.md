`usePoll(key, load, everyMs, done)` drops results for a key the screen has left, so a late answer never lands on a screen that moved on. `useAnswerStream` (use-answer-stream.ts) fetches /api/requests/{id}/stream and parses the events through TextDecoderStream:
- each `data:` event sets provisional `Streamed` content;
- `data: null` withdraws it;
- `event: done` calls `onDone`, which reloads the status poll.
Stream errors are swallowed, because the status poll still delivers the validated outcome.