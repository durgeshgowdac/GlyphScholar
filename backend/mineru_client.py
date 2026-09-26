import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter, Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mineru")


@dataclass
class MinerUConfig:
    """
    All tunable behavior for MinerUClient in one place.

    Build one directly, via `MinerUConfig.from_env()` to read from
    environment variables (MINERU_TOKEN, MINERU_OUTPUT_DIR, ...), or
    pass kwargs straight to MinerUClient() and let it build one for you.
    """

    # Auth / endpoint
    api_key: Optional[str] = None
    base_url: str = "https://mineru.net/api/v4"

    # Output
    output_dir: str = "../output"
    filename_max_length: int = 150
    extract_and_delete_zip: bool = True

    # Extraction request defaults
    model_version: str = "vlm"  # "vlm" or "MinerU-HTML"
    is_ocr: bool = False  # force OCR on every page; off uses the PDF's own text layer where present
    enable_table: bool = True
    enable_formula: bool = True
    language: str = "en"
    page_range: Optional[str] = None  # e.g. "1-10" or "5"; PDF-only, applies to the whole request

    # Polling
    poll_interval: int = 10
    max_poll_time: int = 3600

    # HTTP retry behavior (applies to our own API calls, not presigned URLs)
    max_retries: int = 3
    backoff_factor: float = 1.0
    retry_status_codes: Tuple[int, ...] = (429, 500, 502, 503, 504)

    # Timeouts, in seconds
    submit_timeout: int = 120
    poll_timeout: int = 60
    upload_timeout: int = 300
    download_timeout: int = 300
    head_timeout: int = 15

    @classmethod
    def from_env(cls) -> "MinerUConfig":
        d = cls()  # holds the built-in defaults
        bools = {"1", "true", "yes", "on"}
        return cls(
            api_key=os.environ.get("MINERU_TOKEN"),
            base_url=os.environ.get("MINERU_BASE_URL", d.base_url),
            output_dir=os.environ.get("MINERU_OUTPUT_DIR", d.output_dir),
            filename_max_length=int(os.environ.get("MINERU_FILENAME_MAX_LENGTH", d.filename_max_length)),
            extract_and_delete_zip=os.environ.get("MINERU_EXTRACT_AND_DELETE_ZIP",
                                                  str(d.extract_and_delete_zip)).lower() in bools,
            model_version=os.environ.get("MINERU_MODEL_VERSION", d.model_version),
            is_ocr=os.environ.get("MINERU_IS_OCR", str(d.is_ocr)).lower() in bools,
            enable_table=os.environ.get("MINERU_ENABLE_TABLE", str(d.enable_table)).lower() in bools,
            enable_formula=os.environ.get("MINERU_ENABLE_FORMULA", str(d.enable_formula)).lower() in bools,
            language=os.environ.get("MINERU_LANGUAGE", d.language),
            page_range=os.environ.get("MINERU_PAGE_RANGE", d.page_range),
            poll_interval=int(os.environ.get("MINERU_POLL_INTERVAL", d.poll_interval)),
            max_poll_time=int(os.environ.get("MINERU_MAX_POLL_TIME", d.max_poll_time)),
            max_retries=int(os.environ.get("MINERU_MAX_RETRIES", d.max_retries)),
            backoff_factor=float(os.environ.get("MINERU_BACKOFF_FACTOR", d.backoff_factor)),
            submit_timeout=int(os.environ.get("MINERU_SUBMIT_TIMEOUT", d.submit_timeout)),
            poll_timeout=int(os.environ.get("MINERU_POLL_TIMEOUT", d.poll_timeout)),
            upload_timeout=int(os.environ.get("MINERU_UPLOAD_TIMEOUT", d.upload_timeout)),
            download_timeout=int(os.environ.get("MINERU_DOWNLOAD_TIMEOUT", d.download_timeout)),
            head_timeout=int(os.environ.get("MINERU_HEAD_TIMEOUT", d.head_timeout)),
        )


class MinerUClient:
    """
    Robust, configurable client for the MinerU extraction API.

    Single entry point: `process()`. Pass it any mix of local file paths
    and URLs and it figures out the rest. Output files are named after the
    original source (URL basename or local filename) instead of internal
    task/batch IDs.

    Configuration precedence: explicit kwargs to __init__() > MinerUConfig
    passed in > environment variables (MINERU_*) > built-in defaults.
    """

    def __init__(self, config: Optional[MinerUConfig] = None, **overrides):
        self.config = config or MinerUConfig.from_env()

        if overrides:
            valid_fields = {f.name for f in fields(MinerUConfig)}
            unknown = set(overrides) - valid_fields
            if unknown:
                raise TypeError(f"Unknown MinerUClient config option(s): {sorted(unknown)}")
            for key, value in overrides.items():
                setattr(self.config, key, value)

        if not self.config.api_key:
            raise ValueError("MINERU_TOKEN environment variable is not set.")

        self.output_dir = Path(self.config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Session with automatic retries for transient network/server errors
        self.session = requests.Session()
        retries = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.backoff_factor,
            status_forcelist=list(self.config.retry_status_codes),
            allowed_methods=["GET", "POST", "PUT"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            }
        )

    # ------------------------------------------------------------------
    # Public unified API
    # ------------------------------------------------------------------

    def process(
            self,
            items: Union[str, List[str]],
            model_version: Optional[str] = None,
            is_ocr: Optional[bool] = None,
    ) -> Dict[str, Optional[str]]:
        """
        Process any mix of local file paths and URLs in a single call.

        Args:
            items: a single path/URL or a list of them.
            model_version: overrides config.model_version for this call
                ("vlm" or "MinerU-HTML" for HTML sources).
            is_ocr: overrides config.is_ocr for this call. Forces OCR on
                every page instead of relying on the PDF's own text layer.

        Returns:
            dict mapping each input item -> local extracted folder path, or None if it failed.
        """
        model_version = model_version or self.config.model_version
        extraction_params = self.extraction_params(is_ocr)

        if isinstance(items, str):
            items = [items]

        urls: List[str] = []
        local_files: List[str] = []
        results: Dict[str, Optional[str]] = {}

        for item in items:
            if self.is_url(item):
                urls.append(item)
            else:
                path = Path(item)
                if not path.is_file():
                    logger.error(f"Local file not found, skipping: {item}")
                    results[item] = None
                else:
                    local_files.append(str(path))

        if urls:
            results.update(self.process_urls(urls, model_version, extraction_params))

        if local_files:
            results.update(self.process_local_files(local_files, model_version, extraction_params))

        return results

    def extraction_params(self, is_ocr: Optional[bool] = None) -> dict:
        """PDF-parsing options shared by both the URL and local-file submit payloads."""
        params = {
            "is_ocr": self.config.is_ocr if is_ocr is None else is_ocr,
            "enable_table": self.config.enable_table,
            "enable_formula": self.config.enable_formula,
            "language": self.config.language,
        }
        if self.config.page_range:
            params["page_range"] = self.config.page_range
        return params

    # ------------------------------------------------------------------
    # URL tasks
    # ------------------------------------------------------------------

    @staticmethod
    def is_url(item: str) -> bool:
        return item.startswith("http://") or item.startswith("https://")

    def sanitize_filename(self, name: str) -> str:
        name = re.sub(r"[^\w\-. ]", "_", name).strip().strip(".")
        name = re.sub(r"\s+", "_", name)
        # "Title: Subtitle" collapses a colon+space into "__" above; keep only
        # the primary title before that marker so long subtitles are dropped.
        if "__" in name:
            name = name.split("__", 1)[0]
        name = re.sub(r"_+", "_", name).strip("_.")
        return name[: self.config.filename_max_length] or "download"

    def content_disposition_name(self, url: str) -> Optional[str]:
        """Server-suggested filename, if any, via a lightweight HEAD request."""
        try:
            res = requests.head(url, timeout=self.config.head_timeout, allow_redirects=True)
            disposition = res.headers.get("Content-Disposition", "")
            match = re.search(r'filename="?([^";]+)"?', disposition)
            if match:
                return Path(match.group(1)).stem
        except requests.RequestException:
            pass
        return None

    def derive_output_name(self, url: str) -> str:
        """Human-readable name for a URL: Content-Disposition > URL basename."""
        name = self.content_disposition_name(url) or Path(urlparse(url).path).stem or "download"
        return self.sanitize_filename(name)

    def process_urls(
            self, urls: List[str], model_version: str, extraction_params: dict
    ) -> Dict[str, Optional[str]]:
        results: Dict[str, Optional[str]] = {}
        task_map: Dict[str, tuple] = {}  # task_id -> (url, output_name)

        for url in urls:
            output_name = self.derive_output_name(url)
            try:
                payload = {"url": url, "model_version": model_version, **extraction_params}
                res = self.session.post(
                    f"{self.config.base_url}/extract/task", json=payload, timeout=self.config.submit_timeout
                )
                data = res.json()
            except requests.RequestException as e:
                logger.error(f"Network error submitting {url}: {e}")
                results[url] = None
                continue
            except ValueError:
                logger.error(f"Invalid JSON response submitting {url}: {res.text[:200]}")
                results[url] = None
                continue

            if res.status_code == 200 and data.get("code") == 0:
                task_id = data["data"]["task_id"]
                task_map[task_id] = (url, output_name)
                logger.info(f"Submitted URL task for {url} -> task_id={task_id}")
            else:
                logger.error(f"Failed to submit {url}: {data}")
                results[url] = None

        for task_id, (url, output_name) in task_map.items():
            results[url] = self.poll_task(task_id, output_name)

        return results

    def poll_task(self, task_id: str, output_name: str) -> Optional[str]:
        start = time.time()
        while time.time() - start < self.config.max_poll_time:
            try:
                res = self.session.get(
                    f"{self.config.base_url}/extract/task/{task_id}", timeout=self.config.poll_timeout
                )
                data = res.json().get("data", {})
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"Error polling task {task_id}: {e}, retrying...")
                time.sleep(self.config.poll_interval)
                continue

            state = data.get("state")

            if state == "done":
                zip_url = data.get("full_zip_url")
                local_path = self.output_dir / f"{output_name}.zip"
                logger.info(f"Task {task_id} completed. Downloading -> {local_path}")
                if not self.download_file(zip_url, local_path):
                    return None
                return self.finalize(local_path)
            elif state == "failed":
                logger.error(f"Task {task_id} failed: {data.get('err_msg')}")
                return None
            else:
                progress = data.get("extract_progress", {})
                logger.info(
                    f"Task {task_id} state={state} "
                    f"({progress.get('extracted_pages', 0)}/{progress.get('total_pages', '?')} pages)"
                )
                time.sleep(self.config.poll_interval)

        logger.error(f"Task {task_id} timed out after {self.config.max_poll_time}s")
        return None

    # ------------------------------------------------------------------
    # Local file tasks
    # ------------------------------------------------------------------

    def process_local_files(
            self, file_paths: List[str], model_version: str, extraction_params: dict
    ) -> Dict[str, Optional[str]]:
        results: Dict[str, Optional[str]] = {p: None for p in file_paths}

        files_payload = [{"name": os.path.basename(p)} for p in file_paths]
        payload = {"files": files_payload, "model_version": model_version, **extraction_params}

        try:
            res = self.session.post(
                f"{self.config.base_url}/file-urls/batch", json=payload, timeout=self.config.submit_timeout
            )
            data = res.json()
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Error requesting upload URLs: {e}")
            return results

        if res.status_code != 200 or data.get("code") != 0:
            logger.error(f"Failed to request upload URLs: {data}")
            return results

        upload_urls = data["data"]["file_urls"]
        batch_id = data["data"]["batch_id"]
        logger.info(f"Batch created: {batch_id}")

        uploaded_paths = []
        for path, upload_url in zip(file_paths, upload_urls):
            if self.upload_file(path, upload_url):
                uploaded_paths.append(path)
            else:
                logger.error(f"Skipping {path}: upload failed.")

        if not uploaded_paths:
            logger.error(f"No files uploaded successfully for batch {batch_id}.")
            return results

        logger.info(f"Waiting for batch {batch_id} to process...")
        batch_results = self.poll_batch(batch_id)

        for p in uploaded_paths:
            results[p] = batch_results.get(Path(p).name)

        return results

    def upload_file(self, file_path: str, upload_url: str) -> bool:
        try:
            # Read the whole file into memory rather than passing an open
            # file handle as `data=`: self.session's HTTPAdapter is
            # configured with automatic retries for PUT (see __init__), and
            # urllib3 retries by resending the same body object. A file
            # handle is stateful — a retry after a failed/partial send
            # would resume reading from wherever the first attempt left
            # the cursor (often near EOF), sending a truncated or empty
            # body while Content-Length (fixed at the original file size)
            # still advertises the full length. That mismatch mid-stream is
            # exactly what tends to surface as a TLS record-integrity error
            # (e.g. SSLV3_ALERT_BAD_RECORD_MAC) on retry. Plain bytes have
            # no read position, so every retry — automatic or not — resends
            # the identical, complete body.
            body = Path(file_path).read_bytes()
            res = self.session.put(
                upload_url, data=body, timeout=self.config.upload_timeout,
                headers={"Authorization": None, "Content-Type": None},
            )
            if res.status_code == 200:
                logger.info(f"Uploaded: {file_path}")
                return True
            logger.error(f"Upload failed for {file_path}: HTTP {res.status_code}")
            return False
        except (OSError, requests.RequestException) as e:
            logger.error(f"Error uploading {file_path}: {e}")
            return False

    def poll_batch(self, batch_id: str) -> Dict[str, str]:
        batch_url = f"{self.config.base_url}/extract-results/batch/{batch_id}"
        results: Dict[str, str] = {}
        finished = set()
        start = time.time()

        while time.time() - start < self.config.max_poll_time:
            try:
                res = self.session.get(batch_url, timeout=self.config.poll_timeout)
            except requests.RequestException as e:
                logger.warning(f"Error polling batch {batch_id}: {e}, retrying...")
                time.sleep(self.config.poll_interval)
                continue

            if res.status_code != 200:
                logger.error(f"API error querying batch {batch_id}: {res.status_code} - {res.text[:200]}")
                time.sleep(self.config.poll_interval)
                continue

            try:
                tasks = res.json().get("data", {}).get("extract_result", [])
            except ValueError:
                logger.warning(f"Invalid JSON polling batch {batch_id}, retrying...")
                time.sleep(self.config.poll_interval)
                continue

            if not tasks:
                logger.info("Waiting for batch tasks to initialize...")
                time.sleep(self.config.poll_interval)
                continue

            all_done = True
            for task in tasks:
                file_name = task.get("file_name", "unknown_file")
                state = task.get("state")

                if file_name in finished:
                    continue

                if state == "done":
                    zip_url = task.get("full_zip_url")
                    local_path = self.output_dir / f"{Path(file_name).stem}.zip"
                    logger.info(f"Batch file '{file_name}' done. Downloading -> {local_path}")
                    if self.download_file(zip_url, local_path):
                        results[file_name] = self.finalize(local_path)
                    finished.add(file_name)
                elif state == "failed":
                    logger.error(f"Batch file '{file_name}' failed: {task.get('err_msg')}")
                    finished.add(file_name)
                else:
                    all_done = False
                    logger.info(f"Batch file '{file_name}' state={state}...")

            if all_done and len(tasks) == len(finished):
                logger.info(f"Batch {batch_id} fully processed.")
                break

            time.sleep(self.config.poll_interval)
        else:
            logger.error(f"Batch {batch_id} timed out after {self.config.max_poll_time}s")

        return results

    # ------------------------------------------------------------------
    # Shared
    # ------------------------------------------------------------------

    def download_file(self, url: Optional[str], local_path: Path) -> bool:
        if not url:
            logger.error(f"No download URL provided for {local_path}")
            return False
        try:
            # Plain requests, not self.session: presigned download URLs shouldn't
            # carry our API auth header.
            with requests.get(url, stream=True, timeout=self.config.download_timeout) as r:
                r.raise_for_status()
                tmp_path = local_path.with_suffix(local_path.suffix + ".part")
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                tmp_path.replace(local_path)
            logger.info(f"Saved: {local_path}")
            return True
        except (requests.RequestException, OSError) as e:
            logger.error(f"Failed to download {local_path}: {e}")
            return False

    def finalize(self, zip_path: Path) -> str:
        """Extract-and-delete, or leave the zip alone, per config.extract_and_delete_zip."""
        if not self.config.extract_and_delete_zip:
            return str(zip_path)
        return self.extract_and_cleanup(zip_path)

    def extract_and_cleanup(self, zip_path: Path) -> str:
        """
        Extract zip_path into a same-named folder and delete the zip.
        Returns the extracted folder path, or the zip path itself if
        extraction fails (so the raw archive isn't silently lost).
        """
        extract_dir = zip_path.with_suffix("")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    if os.path.isabs(member) or ".." in Path(member).parts:
                        raise zipfile.BadZipFile(f"Unsafe path in MinerU zip: {member!r}")
                zf.extractall(extract_dir)
            zip_path.unlink()
            logger.info(f"Extracted -> {extract_dir} (removed {zip_path.name})")
            return str(extract_dir)
        except (zipfile.BadZipFile, OSError) as e:
            logger.error(f"Failed to extract {zip_path}, keeping zip: {e}")
            return str(zip_path)


if __name__ == "__main__":
    # Uses MINERU_TOKEN plus any other MINERU_* env vars automatically.
    # Or override directly: MinerUClient(output_dir="./output", poll_interval=5)
    client = MinerUClient()

    results = client.process(
        [
            # "https://arxiv.org/pdf/2606.28344",
            "main.pdf",
            # "pixelrag.pdf",
        ]
    )

    print("\nSummary:")
    for item, path in results.items():
        print(f"  {item} -> {path or 'FAILED'}")
