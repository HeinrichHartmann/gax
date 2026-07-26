# ADR 038: Message-Addressed Mail Replies

**Status:** Accepted
**Date:** 2026-07-26

## Context

You cannot reply to a specific person in a thread with gax today.
`gax mail reply` takes a *thread* and hardwires "reply to the sender of
the last message". UX research on 2026-07-26 with two real threads
showed how badly this breaks. Anonymized, the failing thread looked
like this:

```
1  Me <me@example.com>                                    (own status mail)
2  Mail Delivery Subsystem <mailer-daemon@googlemail.com> (a bounce)
3  Carol <carol@example.com>                              (question to answer)
4  Dave <dave@example.org>                                (unrelated short reply)
```

The goal was to answer Carol (message 3). `gax mail reply` produced:

```yaml
to: Dave <dave@example.org>                # wrong person
in_reply_to: <msg-4@mail.example.org>      # Dave's message
references: <...> <...> <...> <...>        # includes the bounce
```

In a second thread, where the last message was a bounce, the draft was
addressed to `mailer-daemon@googlemail.com`. The only workaround is
hand-editing `to:`, `in_reply_to:` and `references:` in the draft YAML,
which requires understanding RFC 2822 threading internals.

The root cause is a modeling error: **replies are to messages, not to
threads**. The thread is context; the thing you answer is one specific
message. The CLI has no way to name a message, and our files do not
even show an ID you could name it by.

## Research: what Gmail gives us

Verified live against a real thread (2026-07-26); values below are
anonymized but structurally exact.

### Per message, from the API

`threads.get` returns full data for every message; `messages.get` works
with a message ID directly. Each message carries:

| Field | Example (message 3, Carol) | Note |
|---|---|---|
| `id` | `19f9ae1f6df20003` | Gmail message hex ID, same 16-hex shape as thread IDs |
| `threadId` | `19f9a45b51e50000` | so a message ID alone recovers its thread |
| `From` | `Carol <carol@example.com>` | |
| `To`, `Cc` | `Me <me@example.com>` | full recipient lists — enough for reply-all |
| `Reply-To` | (when sender sets it) | the RFC-correct reply target; gax ignores it today |
| `Message-ID` | `<msg-3@mail.example.com>` | RFC 2822 ID for threading headers |
| `In-Reply-To` | `<msg-1@mail.example.com>` | what *this* message answered |
| `References` | `<msg-1@mail.example.com>` | the message's own ancestor chain |
| `labelIds`, `internalDate`, `snippet` | `INBOX, IMPORTANT, ...` | |

Two important consequences:

1. **We do not need to build the References chain ourselves.** The
   correct chain for a reply to message M is `M.References +
   M.Message-ID` [^rfc]. Today's code concatenates the Message-IDs of
   *all* thread sections, which wrongly includes bounces and messages
   *later* than the reply target.
2. **A message hex ID is a complete address.** `messages.get(id)`
   returns the thread and every header needed to construct a correct
   reply. Nothing else is required from the caller.

### From the Gmail web UI

| Source | What you get | Status |
|---|---|---|
| DOM attribute `data-legacy-message-id` | message hex ID of each rendered message | works; bookmarklet-friendly |
| DOM attribute `data-legacy-thread-id` | thread hex ID | works; current bookmarklet uses it |
| "Show original" URL `permmsgid=msg-f:<decimal>` | message ID in decimal (= hex ID) | works, zero setup |
| Copied URL `#inbox/Ktbx...` | encrypted per-account token | **unusable** — no API mapping exists [^cli858] |

So the web UI leaks message IDs in several places even though the URL
token is a dead end. A bookmarklet can put a message hex ID on the
clipboard with one click.

## Decision (proposed)

Make the message the unit of addressing throughout the mail commands.

### 1. Messages get IDs in our files and output

Every section of a `.mail.gax.md` (and of `gax get` thread output)
carries the Gmail message hex ID, which `pull_thread` already receives
and currently drops:

```yaml
section: 3
id: 19f9ae1f6df20003
from: Carol <carol@example.com>
date: 2026-07-25T22:05:28Z
message_id: <msg-3@mail.example.com>
```

`id` is the address you use on the CLI; `message_id` stays what it is
(RFC 2822 header material).

### 2. `gax mail reply` takes a message

```bash
gax mail reply 19f9ae1f6df20003          # message hex ID (from file, output, or bookmarklet)
gax mail reply thread.mail.gax.md        # file: replies to the last message
```

The message hex ID is the *only* way to address a specific message.
There is no section-number indexing (`-m 3` or similar): section
numbers shift on every pull, while the ID sits directly next to the
message in the file and in `gax get` output. Thread-level input
(file, thread URL, thread ID) means "reply to the last message" —
plain and predictable, like Gmail's own reply button; anything else
is addressed by ID.

Resolution rule for a bare 16-hex argument: try `messages.get` first;
if that 404s, fall back to `threads.get` (= last message). One
subtlety: a Gmail thread ID is often identical to its *first*
message's ID, so `messages.get` can succeed for what the user meant
as a thread. Disambiguation: if the resolved message's `id` equals
its `threadId` (it is the thread anchor), treat the input as a thread
reference — reply to the last message, not the first. Thread files
and thread URLs keep working and mean "the last message".

### 3. Headers derive from the target message only

```yaml
to:          target.Reply-To or target.From
cc:          (only with a future --all flag: target.To + target.Cc minus self)
subject:     target.Subject, prefixed with "Re: " if missing
in_reply_to: target.Message-ID
references:  target.References + target.Message-ID
thread_id:   target.threadId
```

This fixes three current bugs at once: replying to bounces, including
later messages in `references`, and ignoring `Reply-To`.

### 4. Bookmarklet

The companion bookmarklet copies the `data-legacy-message-id` of the
expanded message (falling back to the thread ID if none), so the
Gmail web UI → `gax mail reply <id>` path is one click.

## Out of scope

Reply-all flag, `--body`/stdin input, draft send, quoted original text.
Each is a separate decision after this lands.

## Consequences

- `Thread.reply()` moves from thread-tail logic to message-target
  logic; the References-chain builder in `thread.py:251-257` is
  replaced by the target message's own headers.
- `.mail.gax.md` files gain one header line per section (backward
  compatible: readers ignore unknown headers; old files without `id`
  still support file-level reply via `message_id` lookup).
- `gax get`/clone output becomes self-describing for replies: you see
  who wrote what and the ID to answer it with.

[^rfc]: RFC 5322 §3.6.4: the reply's References field is the parent's
    References plus the parent's Message-ID.

[^cli858]: googleworkspace/cli#858 — modern Gmail web IDs (`Ktbx...`,
    `FMfcg...`) are encrypted per-account; no offline or API mapping
    to hex IDs exists.
