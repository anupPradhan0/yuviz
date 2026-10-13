# Knowledge document parsing: what the sandbox is and is not

> **PDF and Office parsing (PDF, DOCX, PPTX, XLSX) is OFF by default.** The security audit and two independent reviews
> concluded it must not run in the same-uid, uncontained child for untrusted tenants. An operator turns it on with
> `KNOWLEDGE_BINARY_PARSERS=1` (also `true`, `yes`, `on`) once the contained-worker deployment under "Real containment"
> below exists, or after accepting the risk in writing. Any other value is OFF and logs one warning. The switch is read in
> one place (`parsers/limits.py: binary_parsers_enabled()`) by the parser, the upload route and the worker. When off, a
> PDF/Office upload is refused at the route with 415 and "PDF and Office documents are not enabled on this deployment", and
> an already-stored one fails in the worker with the same reason. The test suite sets it to 1 in `tests/conftest.py`; CI must
> export `KNOWLEDGE_BINARY_PARSERS=1` for the knowledge test step if it does not run the suite through that conftest.
>
> | | `KNOWLEDGE_BINARY_PARSERS=1` (opt in) | unset or `=0` (default) |
> |---|---|---|
> | PDF, DOCX, PPTX, XLSX | parsed (pypdf, python-docx, python-pptx, openpyxl, Pillow, optional `tesseract`) | refused with "PDF and Office documents are not enabled on this deployment" |
> | HTML | BeautifulSoup on **lxml/libxml2** | BeautifulSoup on the pure-Python `html.parser`; same hidden-content stripping and limits |
> | CSV, TSV, markdown, text | pure Python | pure Python |
> | Native code reachable from a hostile file | libxml2 (lxml), Pillow, the OCR binary, plus the interpreter | the Python interpreter and its standard library only |
>
> With the switch at 0 the child blocks `import lxml` outright (a test proves lxml is never loaded), pypdf and the Office
> libraries are never imported, and no XML library is used. The parsers are still in the same uncontained child process
> (see below), so this lowers the attack surface; it does not contain it.

Uploaded documents are parsed by `services/knowledge/parsers/` in a child process. **This is resource isolation, not a
security sandbox.** A document that exploits a parser library (lxml, Pillow, pypdf, python-docx, openpyxl, BeautifulSoup)
gets code execution as the same OS user as the ingestion worker, with that user's files, secrets, database access and
network. Measured on macOS with the production `spawn()` and `harden()`, a compromised child:
- read the parent's `POSTGRES_DSN` (`ps eww -p $PPID`), the repo `.env`, and listed `data/knowledge_documents/`
- connected to Postgres on 127.0.0.1 and to the internet
- started a `fork()+setsid()` grandchild that survived the process-group kill

## What the child gets
- `python -I` (isolated: no cwd, script dir, PYTHONPATH or user site on `sys.path`), cwd = a fresh empty temp dir removed afterwards,
  `close_fds`, its own session and process group
- an environment of `PATH`, `LANG` and `KB_PARSE_NONCE` only (the nonce is not a secret; it tags the child's own log notes)
- a wall-clock timeout, an output byte cap, and two memory guards that work on macOS and Linux: the parent polls the RSS of the
  **whole process group** every 20 ms (`ps -axo pgid=,rss=`), and the child exits by itself (code 75) within about 5 ms of its peak RSS passing
  the limit (default 1024 MB, minimum 256 MB; a lower `KB_PARSE_MEMORY_MB` is raised with a warning). A long C call that holds the
  GIL delays the child guard; the parent still covers it. Any breach SIGKILLs the process group
- size pre-checks before anything is built: decompressed bytes, XLSX declared cells, CSV columns and cells, DOCX/PPTX paragraphs, a
  per-format element caps (DOCX/PPTX 2.5M XML elements, XLSX 3x its 2M-cell cap, HTML 500k; ordinary 1,000-page documents and 30k-row spreadsheets are far below them), HTML nesting depth (real open-tag nesting) and stylesheet size/rules/selectors/time, PDF page content and repeated shared streams, characters
- child stderr is never logged verbatim: only lines carrying this run's nonce are logged (escaped, capped), and a failure logs the
  return code or signal and the final exception class only
- Linux only, best effort, each failure reported and parsing continues: `RLIMIT_CPU/FSIZE/CORE/NOFILE/AS`, `RLIMIT_NPROC=1` (only when no
  `tesseract` is installed: OCR forks), `PR_SET_NO_NEW_PRIVS`, `PR_SET_DUMPABLE=0`, and `unshare(CLONE_NEWUSER|CLONE_NEWNET)` then plain
  `unshare(CLONE_NEWNET)` to drop the network
- the worker process sets `PR_SET_DUMPABLE=0` at startup (Linux), and `KNOWLEDGE_STORAGE_ROOT` is resolved to an absolute path

## What it does not stop
- **Same-uid access.** The child can read every file its uid can: the document store of every tenant, `.env` files, and the
  environment/memory of sibling processes. `PR_SET_DUMPABLE=0` protects only the ingestion worker, and **only on Linux**; on macOS the
  parent's environment is readable (measured). The Knowledge API (which holds `JWT_SECRET` and the DSN) runs under the same uid in
  `scripts/start_local.sh` and is not made non-dumpable, so a child can read its `/proc/<pid>/environ`.
- **The network survives in every realistic deployment.** `unshare(CLONE_NEWUSER|CLONE_NEWNET)` needs unprivileged user namespaces enabled
  (many hardened kernels and Docker's default seccomp profile refuse it); plain `CLONE_NEWNET` needs `CAP_SYS_ADMIN`. When both fail the
  child keeps full network access, including the database. Not tested on Linux here.
- **Escape from the group kill.** A child can `fork()+setsid()` a grandchild out of the process group. `RLIMIT_NPROC=1` would stop that, but the
  kernel does not enforce it for uid 0, and `deployment/docker/Dockerfile.python` has no `USER`, so services run as root; it is also skipped
  when `tesseract` is installed.
- A compromised child can `kill(getppid())`; each reap and retry repeats it up to `max_attempts`.
- The repo root is passed to the child (it must import the parser code from there); removing it from argv would hide nothing, because the
  code path is on `sys.path` and readable anyway. Not changed.

## Real containment (the deployment change that is actually required)
Run parsing as a separate service, not as a child of the worker. Concretely:
1. **A separate image/container** `knowledge-parser` containing only the parser code and its libraries (no `.env`, no DB driver config, no
   `services/` secrets). Entry point: read one document on stdin, write the JSON result on stdout (the `run_parse()` contract is already that).
2. **A dedicated non-root uid**, e.g. in its Dockerfile: `RUN useradd -u 10001 -M parser` and `USER 10001:10001`. (`Dockerfile.python` itself needs a
   `USER` line for the API and worker images too; that file is not changed in this PR.)
3. **No network**: `network_mode: none` (compose) / `--network none`, so a compromised parser cannot reach Postgres or the internet.
4. **Read-only root filesystem**, a small `tmpfs` for `/tmp`, **no volumes**, no secrets, no environment beyond `PATH`.
5. **cgroup limits** (memory, CPU, pids) and the default seccomp profile (`--cap-drop ALL --security-opt no-new-privileges`).
6. The worker starts that container per document (or talks to a pool over a unix socket / `docker run -i`), passes bytes in over stdin and
   enforces its own timeout. The worker never mounts the document store into the parser.

Docker Compose sketch (illustrative; not added to the repo):
```yaml
services:
  knowledge-parser:
    image: yuviz/knowledge-parser
    user: "10001:10001"
    network_mode: none
    read_only: true
    tmpfs: ["/tmp:size=64m,noexec,nosuid"]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 1g
    pids_limit: 64
    environment: {}            # nothing
    volumes: []                # nothing
    stdin_open: true           # stdin/stdout only
```
Until this is in place, treat PDF/Office parsing as untrusted-code execution on the worker host: keep `KNOWLEDGE_BINARY_PARSERS=0` for any tenant you do not
trust, and get the risk accepted in writing for any tenant you do.

PDF pages heavier than `KB_PARSE_PDF_PAGE_BYTES` (2 MB of decoded content), pages sharing a large stream, and extraction past `KB_PARSE_PDF_BUDGET_S` (25 s) are skipped
rather than rejected: the rest of the document is kept and marked `ready_degraded` with the reason (a PDF with nothing readable still fails). The memory watchdog polls
fast only while memory is large or growing and backs off to 0.5 s when it is small and flat (reading `/proc` on Linux, `ps` elsewhere), because the child's own guard is the
fast backstop; polling `ps` every 20 ms cost about half a core per parse on the test host.

## Content that is dropped on purpose
Hidden or invisible content is removed by default and counted in the `extract` ingestion event (`hidden_removed`). This is best effort, not a guarantee:
- **HTML (collapsed content):** closed `<details>`, `aria-expanded=false` panels, Bootstrap `.collapse` panels and hidden tab panels are dropped by default and counted as `html_collapsed`, separately from `html_hidden_elements`. This is a policy choice: a FAQ answer behind an accordion is real content, but collapsing is also the classic way to hide injected text. Set `KNOWLEDGE_KEEP_COLLAPSED_CONTENT=1` to index them (plain `display:none` text is still dropped).
- **HTML:** `hidden`, `aria-hidden`, `<input type=hidden>`; inline styles and `<style>` rules (matched with CSS selectors, specificity not modelled, `@media` ignored) for
  `display:none`, `visibility:hidden`, near-zero `opacity`, tiny or zero `font-size`, `color:transparent`, `clip`/`clip-path`, large negative offsets and `text-indent`, zero-size with
  `overflow:hidden`, and white text where no ancestor paints a non-white background. There is no tag-stripping fallback (it cannot see CSS and would index exactly this text): a page the
  main parser cannot handle is rejected.
- **DOCX:** hidden runs, tracked deletions, white text without a dark shading (direct formatting).
- **PPTX:** hidden slides, speaker notes (opt in with `KNOWLEDGE_INDEX_SPEAKER_NOTES=1`).
- **XLSX:** hidden and veryHidden sheets.
- **PDF:** render-mode-3/7 text, tiny text and white-and-small text.

Known limits: white-on-white detection is a heuristic (it cannot see page backgrounds beyond what the markup sets); content hidden by covering shapes, off-page placement in
non-HTML formats, or layers is kept; hiding through DOCX styles, PPTX selection-pane/off-slide shapes, XLSX font colours/number formats and PDF optional-content layers is covered only where
listed in the PR description for this round; invisible PDF text in CID-keyed fonts is not located; hidden XLSX rows/columns are kept. Do not rely on this as a prompt-injection defence:
retrieved text is data and must be treated as untrusted by the agent prompt.
