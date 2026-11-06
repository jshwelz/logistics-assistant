import os
import signal
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings
from app.errors import PermanentIngestionError, TransientIngestionError


def convert_docx_to_pdf(source: Path, destination: Path, settings: Settings) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo-job-") as tmp:
        root = Path(tmp)
        profile = root / "profile"
        outdir = root / "out"
        profile.mkdir()
        outdir.mkdir()
        command = [
            settings.libreoffice_bin,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nologo",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(outdir),
            str(source),
        ]
        env = os.environ.copy()
        env["HOME"] = str(profile)
        env["SAL_USE_VCLPLUGIN"] = "svp"
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=root,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise PermanentIngestionError("conversion_failed") from None
        try:
            code = process.wait(timeout=settings.libreoffice_timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            raise TransientIngestionError("conversion_timeout") from None
        if code != 0:
            raise TransientIngestionError("conversion_failed") from None
        produced = [path for path in outdir.glob("*.pdf") if path.is_file() and path.stat().st_size > 0]
        if len(produced) != 1:
            raise PermanentIngestionError("conversion_failed")
        partial = destination.with_name(destination.name + ".partial")
        partial.write_bytes(produced[0].read_bytes())
        os.replace(partial, destination)


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
