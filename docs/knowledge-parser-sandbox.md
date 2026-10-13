# Knowledge document parsing: what the sandbox is and is not

Uploaded documents are parsed by `services/knowledge/parsers/` in a child process. **This is resource isolation, not a
security sandbox.** A document that exploits a parser library (lxml, Pillow, pypdf, python-docx, openpyxl) gets code
execution as the same OS user as the ingestion worker.

## What the child gets (all platforms unless noted)
- `python -I` (isolated: no cwd, PYTHONPATH or user site on `sys.path`), cwd = a fresh empty temp dir removed afterwards
- environment of `PATH` and `LANG` only; `close_fds`; own session and process group
- timeout, output byte cap and an RSS watchdog (polls `ps`); a breach kills the whole process group
- hostile-size pre-checks before anything is built (cells, columns, paragraphs, decompressed bytes, characters)
- Linux only, best effort, each failure logged as `parse sandbox hardening: ...` and parsing continues:
  `RLIMIT_AS`/`CPU`/`FSIZE=0`/`CORE`/`NOFILE`, `RLIMIT_NPROC=1` (only when no `tesseract` is installed, because OCR
  forks), `PR_SET_NO_NEW_PRIVS`, `PR_SET_DUMPABLE=0`, and a private empty network namespace via `unshare(CLONE_NEWNET)`
  when the kernel permits it
- the worker process sets `PR_SET_DUMPABLE=0` at startup (Linux), so a same-uid child cannot read its `/proc/<pid>/environ`
  or memory; `KNOWLEDGE_STORAGE_ROOT` is resolved to an absolute path at startup

## What it does not stop
A compromised parser can still read any file the worker's uid can read (including `./data/knowledge_documents/<tenant>/`
and `.env` files), and can use the network wherever `unshare` is refused (unprivileged containers, macOS). macOS gets
none of the Linux-only items; its memory cap is the RSS watchdog alone. The Linux-only paths are tested on Linux CI only.

## Deployment change that gives real containment
Run parsing as a separate service or container, not as a child of the worker: its own uid, read-only root filesystem,
no network, no mounted secrets, no access to the document store except the one document handed to it over stdin, a
seccomp profile, and cgroup memory/CPU/pids limits. The worker then talks to it over a pipe or unix socket. The
`run_parse()` interface (bytes in, JSON out) was kept narrow so that move does not change the parser code.

## Content that is dropped on purpose
Hidden or invisible content never reaches the index (and so never reaches the LLM): HTML `display:none`/`hidden`/
`aria-hidden`/zero-size and white-and-tiny text; DOCX hidden runs, tracked deletions, white text without a dark fill;
PPTX hidden slides and speaker notes (notes are opt-in with `KNOWLEDGE_INDEX_SPEAKER_NOTES=1`); XLSX hidden and
veryHidden sheets; PDF render-mode-3 text and tiny or white-and-small text. The count removed is in the `extract`
ingestion event (`hidden_removed`). Residual limits: white-on-white detection is a heuristic (it cannot see page
backgrounds, so white text over a white page at normal size is kept); text hidden by being off-page or covered by
shapes is kept; PDF invisible text in CID-keyed fonts is not located; hidden XLSX rows/columns and PPTX shapes hidden
by animation are kept.
