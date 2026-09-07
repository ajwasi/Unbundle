"""Invokes the real, unmodified humble-cli binary for the actual download step
— confirmed grammar against the real binary (2026-09-06, v0.23.2):
`humble-cli download <gamekey> -i 1,3,7` downloads only those 1-based
subproduct indices; omitting -i downloads everything; already-correct-size
files are silently skipped (its own resume logic), confirmed by re-running a
multi-item download after one item already existed on disk.

`-f FORMAT` (repeatable) filters by format name and unions across repeats —
confirmed `-i 2 -f EPUB` downloads only the epub and prints "Skipping 'PDF'"
for the other variant, and `-i 1 -f EPUB -f PDF` downloads both. -i and -f
compose freely; omitting -i applies a format filter across the whole bundle.

Never parse stdout for success/failure — app/downloads/worker.py verifies via
the filesystem instead (see paths.py). Output is only kept for error
diagnostics when the process exits non-zero.
"""

import asyncio
import os
from pathlib import Path


async def run_download_job(
    humble_cli_path: str,
    gamekey: str,
    indices: list[int] | None,
    formats: list[str] | None,
    cwd: Path,
) -> tuple[int, str]:
    cwd.mkdir(parents=True, exist_ok=True)
    argv = [humble_cli_path, "download", gamekey]
    if indices:
        argv += ["-i", ",".join(str(i) for i in sorted(set(indices)))]
    for fmt in sorted(set(formats or [])):
        argv += ["-f", fmt]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=os.environ.copy(),  # inherit $HOME so ~/.humble-cli-key resolves the same as this process
    )
    output = await proc.stdout.read()
    await proc.wait()
    return proc.returncode, output.decode(errors="replace")
