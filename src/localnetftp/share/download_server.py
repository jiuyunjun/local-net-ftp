from __future__ import annotations

import locale
import ipaddress
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable

from flask import Flask, abort, request, send_file
from werkzeug.serving import make_server

from localnetftp.transfer import available_destination_path


DEFAULT_SHARE_PORT = 49300
IPCONFIG_TIMEOUT_SECONDS = 1.5
_LOCAL_IPV4_INTERFACES_CACHE: list[tuple[str, str]] | None = None
_LOCAL_IPV4_INTERFACES_CACHE_AT = 0.0
INTERFACE_CACHE_SECONDS = 5.0


@dataclass(frozen=True)
class ShareAddress:
    interface_name: str
    address: str
    url: str


def lan_download_urls(port: int, filename: str, addresses: list[str] | None = None) -> list[ShareAddress]:
    interfaces = (
        [("局域网", address) for address in addresses]
        if addresses is not None
        else local_ipv4_interfaces()
    )
    return [
        ShareAddress(interface_name=interface_name, address=address, url=f"http://{address}:{port}/")
        for interface_name, address in interfaces
    ]


def local_ipv4_addresses() -> list[str]:
    return [address for _, address in local_ipv4_interfaces()]


def local_ipv4_interfaces() -> list[tuple[str, str]]:
    global _LOCAL_IPV4_INTERFACES_CACHE, _LOCAL_IPV4_INTERFACES_CACHE_AT
    now = time.monotonic()
    if _LOCAL_IPV4_INTERFACES_CACHE is not None and now - _LOCAL_IPV4_INTERFACES_CACHE_AT < INTERFACE_CACHE_SECONDS:
        return list(_LOCAL_IPV4_INTERFACES_CACHE)

    ipconfig_interfaces = _windows_ipconfig_interfaces()
    if ipconfig_interfaces:
        _LOCAL_IPV4_INTERFACES_CACHE = ipconfig_interfaces
        _LOCAL_IPV4_INTERFACES_CACHE_AT = now
        return ipconfig_interfaces

    addresses: set[str] = set()
    host_name = socket.gethostname()

    try:
        for result in socket.getaddrinfo(host_name, None, socket.AF_INET, socket.SOCK_STREAM):
            address = result[4][0]
            if _is_lan_address(address):
                addresses.add(address)
    except socket.gaierror:
        pass

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if _is_lan_address(address):
                addresses.add(address)
    except OSError:
        pass

    interfaces = [("局域网", address) for address in sorted(addresses)]
    _LOCAL_IPV4_INTERFACES_CACHE = interfaces
    _LOCAL_IPV4_INTERFACES_CACHE_AT = now
    return interfaces


class DownloadShareServer:
    def __init__(self, file_path: Path, port: int = DEFAULT_SHARE_PORT, host: str = "0.0.0.0") -> None:
        self.file_path = file_path
        self.port = port
        self.host = host
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.file_path.exists():
            raise FileNotFoundError(self.file_path)

        app = Flask(__name__)

        @app.get("/")
        def index():
            filename = escape(self.file_path.name)
            return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalNetFTP 下载</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
      background: #f4f7fb;
      color: #172033;
    }}
    main {{
      width: min(420px, calc(100vw - 32px));
      padding: 28px;
      border: 1px solid #d8e0ea;
      border-radius: 14px;
      background: white;
      box-shadow: 0 18px 50px rgba(31, 45, 61, 0.12);
      text-align: center;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 22px;
    }}
    p {{
      margin: 0 0 22px;
      color: #5b687a;
    }}
    a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 0 20px;
      border-radius: 8px;
      background: #2f7dd1;
      color: white;
      text-decoration: none;
      font-weight: 600;
    }}
    a:hover {{
      background: #2269b5;
    }}
  </style>
</head>
<body>
  <main>
    <h1>LocalNetFTP</h1>
    <p>{filename}</p>
    <a href="/download">下载 Windows 版</a>
  </main>
</body>
</html>"""

        @app.get("/download")
        def download():
            return send_file(self.file_path, as_attachment=True, download_name=self.file_path.name)

        self._server = make_server(self.host, self.port, app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LocalNetFTPDownloadShare",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def urls(self) -> list[ShareAddress]:
        return lan_download_urls(self.port, self.file_path.name)


class MobileFileShareServer:
    def __init__(self, paths: list[Path], port: int = DEFAULT_SHARE_PORT + 1, host: str = "0.0.0.0") -> None:
        self.paths = [path.resolve() for path in paths]
        self.port = port
        self.host = host
        self._server = None
        self._thread: threading.Thread | None = None
        self._files: dict[str, Path] = {}
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._files = _share_files(self.paths)
        if not self._files:
            raise FileNotFoundError("No files to share.")

        app = Flask(__name__)

        @app.get("/")
        def index():
            items = "\n".join(
                _mobile_share_item_html(file_id, path)
                for file_id, path in self._files.items()
            )
            return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalNetFTP 文件下载</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
      background: #f4f7fb;
      color: #172033;
    }}
    main {{
      max-width: 720px;
      margin: 0 auto;
      padding: 28px 18px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
    }}
    p {{
      margin: 0 0 18px;
      color: #5b687a;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }}
    button {{
      min-height: 40px;
      padding: 0 14px;
      border: 1px solid #9aa8ba;
      border-radius: 8px;
      background: white;
      color: #172033;
      font-weight: 600;
    }}
    button.primary {{
      border-color: #2f7dd1;
      background: #2f7dd1;
      color: white;
    }}
    .file {{
      display: flex;
      align-items: center;
      gap: 14px;
      min-height: 54px;
      margin-bottom: 10px;
      padding: 0 14px;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
      background: white;
      color: #172033;
      text-decoration: none;
      box-shadow: 0 10px 26px rgba(31, 45, 61, 0.08);
    }}
    .file input {{
      width: 20px;
      height: 20px;
      flex: 0 0 auto;
    }}
    .file span {{
      flex: 1 1 auto;
      overflow-wrap: anywhere;
      font-weight: 600;
    }}
    .file small {{
      flex: 0 0 auto;
      color: #64748b;
    }}
    .preview {{
      margin: -4px 0 12px 34px;
      padding: 12px;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
      background: white;
    }}
    .preview img {{
      display: block;
      max-width: 100%;
      max-height: 70vh;
      object-fit: contain;
      border-radius: 6px;
    }}
    .text-preview {{
      max-height: 45vh;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 0 0 10px;
      color: #172033;
    }}
  </style>
</head>
<body>
  <main>
    <h1>LocalNetFTP</h1>
    <p>选择文件下载</p>
    <div class="actions">
      <button type="button" onclick="selectAll()">全选</button>
      <button type="button" class="primary" onclick="downloadSelected()">下载选中</button>
      <button type="button" onclick="downloadZip()">打包 ZIP</button>
    </div>
    {items}
  </main>
  <script>
    function selectedIds() {{
      return Array.from(document.querySelectorAll('input[type="checkbox"]:checked')).map(item => item.value);
    }}
    function selectAll() {{
      const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
      const shouldCheck = boxes.some(item => !item.checked);
      boxes.forEach(item => item.checked = shouldCheck);
    }}
    function isSafari() {{
      const ua = navigator.userAgent;
      return /Safari/i.test(ua) && !/Chrome|Chromium|CriOS|FxiOS|Edg|OPR/i.test(ua);
    }}
    function downloadSelected() {{
      const ids = selectedIds();
      if (ids.length === 0) return;
      if (ids.length === 1) {{
        window.location.href = '/download/' + encodeURIComponent(ids[0]);
        return;
      }}
      if (isSafari()) {{
        window.location.href = '/download.zip?ids=' + encodeURIComponent(ids.join(','));
        return;
      }}
      ids.forEach((id, index) => {{
        window.setTimeout(() => {{
          const link = document.createElement('a');
          link.href = '/download/' + encodeURIComponent(id);
          link.download = '';
          document.body.appendChild(link);
          link.click();
          link.remove();
        }}, index * 250);
      }});
    }}
    function downloadZip() {{
      const ids = selectedIds();
      if (ids.length === 0) return;
      window.location.href = '/download.zip?ids=' + encodeURIComponent(ids.join(','));
    }}
    async function copyText(id) {{
      const text = document.getElementById(id)?.textContent || '';
      if (!text) return;
      let copied = false;
      if (navigator.clipboard && window.isSecureContext) {{
        try {{
          await navigator.clipboard.writeText(text);
          copied = true;
        }} catch (error) {{
          copied = false;
        }}
      }}
      if (!copied) {{
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.left = '-9999px';
        document.body.appendChild(area);
        area.select();
        copied = document.execCommand('copy');
        area.remove();
      }}
      const button = document.querySelector('[data-copy-target="' + id + '"]');
      if (!button) return;
      const original = button.textContent;
      button.textContent = copied ? '已复制' : '复制失败';
      window.setTimeout(() => button.textContent = original, 1400);
    }}
  </script>
</body>
</html>"""

        @app.get("/download/<file_id>")
        def download(file_id: str):
            path = self._files.get(file_id)
            if path is None or not path.exists():
                abort(404)
            return send_file(path, as_attachment=True, download_name=path.name)

        @app.get("/preview/<file_id>")
        def preview(file_id: str):
            path = self._files.get(file_id)
            if path is None or not path.exists() or not _is_mobile_inline_image(path):
                abort(404)
            return send_file(path)

        @app.get("/download.zip")
        def download_zip():
            paths = self._selected_paths(request.args.get("ids", ""))
            if not paths:
                abort(404)
            zip_path = self._make_zip(paths)
            return send_file(zip_path, as_attachment=True, download_name=zip_path.name)

        self._server = make_server(self.host, self.port, app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LocalNetFTPMobileShare",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
            self._temp_dir = None

    def urls(self) -> list[ShareAddress]:
        return lan_download_urls(self.port, "LocalNetFTP", None)

    def _selected_paths(self, raw_ids: str) -> list[Path]:
        if not raw_ids:
            return []
        paths: list[Path] = []
        for file_id in raw_ids.split(","):
            path = self._files.get(file_id.strip())
            if path is not None and path.exists():
                paths.append(path)
        return paths

    def _make_zip(self, paths: list[Path]) -> Path:
        if self._temp_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="localnetftp-mobile-")
        zip_path = Path(self._temp_dir.name) / "LocalNetFTP-files.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            used_names: set[str] = set()
            for path in paths:
                archive.write(path, _zip_arcname(path, used_names))
        return zip_path


MobileReceiveCallback = Callable[[list[Path]], None]


class MobileReceiveServer:
    def __init__(
        self,
        receive_dir: Path,
        port: int = DEFAULT_SHARE_PORT + 2,
        host: str = "0.0.0.0",
        on_received: MobileReceiveCallback | None = None,
    ) -> None:
        self.receive_dir = receive_dir
        self.port = port
        self.host = host
        self._on_received = on_received
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self.receive_dir.mkdir(parents=True, exist_ok=True)
        app = Flask(__name__)

        @app.get("/")
        def index():
            return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LocalNetFTP 上传</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
      background: #f4f7fb;
      color: #172033;
    }
    main {
      max-width: 720px;
      margin: 0 auto;
      padding: 28px 18px;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 24px;
    }
    p {
      margin: 0 0 18px;
      color: #5b687a;
    }
    form {
      display: grid;
      gap: 12px;
    }
    textarea,
    input[type="file"] {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #d8e0ea;
      border-radius: 8px;
      background: white;
      color: #172033;
      font: inherit;
    }
    textarea {
      min-height: 150px;
      padding: 12px;
      resize: vertical;
    }
    input[type="file"] {
      padding: 12px;
    }
    button {
      min-height: 42px;
      padding: 0 16px;
      border: 1px solid #2f7dd1;
      border-radius: 8px;
      background: #2f7dd1;
      color: white;
      font-weight: 600;
    }
    output {
      min-height: 22px;
      color: #536173;
    }
  </style>
</head>
<body>
  <main>
    <h1>LocalNetFTP</h1>
    <p>选择文件、图片，或输入文字发送到电脑。</p>
    <form id="uploadForm">
      <input name="files" type="file" multiple>
      <textarea name="text" placeholder="输入或粘贴文字，也可在这里粘贴图片"></textarea>
      <small>支持单个或多个文件。若手机浏览器不支持粘贴图片，请用上方文件选择器选择照片。</small>
      <div id="pasted"></div>
      <button type="submit">发送到电脑</button>
      <output id="status"></output>
    </form>
  </main>
  <script>
    const form = document.getElementById('uploadForm');
    const status = document.getElementById('status');
    const pasted = document.getElementById('pasted');
    const images = [];
    form.elements.text.addEventListener('paste', event => {
      const files = Array.from(event.clipboardData?.files || []).filter(file => file.type.startsWith('image/'));
      if (!files.length) return;
      event.preventDefault();
      const text = event.clipboardData.getData('text/plain');
      if (text) form.elements.text.setRangeText(text, form.elements.text.selectionStart, form.elements.text.selectionEnd, 'end');
      files.forEach(file => {
        images.push(file);
        const row = document.createElement('div');
        const preview = document.createElement('img');
        const url = URL.createObjectURL(file);
        preview.src = url;
        preview.alt = file.name || '粘贴图片';
        preview.style.cssText = 'max-width:100%;max-height:160px;display:block;margin:8px 0';
        preview.onload = () => URL.revokeObjectURL(url);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '移除图片';
        remove.onclick = () => { images.splice(images.indexOf(file), 1); row.remove(); };
        row.append(preview, remove);
        pasted.append(row);
      });
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      if (button.disabled) return;
      const body = new FormData(form);
      images.forEach((file, index) => body.append('files', file, file.name || ('粘贴图片_' + index + '.png')));
      if (!Array.from(body.getAll('files')).some(file => file.name) && !form.elements.text.value.trim()) {
        status.textContent = '请选择文件、粘贴图片或输入文字';
        return;
      }
      const controls = Array.from(form.querySelectorAll('input, textarea, button'));
      controls.forEach(control => control.disabled = true);
      status.textContent = '正在发送...';
      try {
        const response = await fetch('/upload', {method: 'POST', body});
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message || '发送失败');
        status.textContent = payload.message || '已保存到电脑';
        form.reset();
        images.length = 0;
        pasted.replaceChildren();
      } catch (error) {
        status.textContent = '发送失败：' + error.message + '。内容已保留，请检查连接后重试。';
      } finally {
        controls.forEach(control => control.disabled = false);
      }
    });
  </script>
</body>
</html>"""

        @app.post("/upload")
        def upload():
            saved_paths: list[Path] = []
            for storage in request.files.getlist("files"):
                if not storage.filename:
                    continue
                destination = _available_mobile_upload_path(self.receive_dir, storage.filename)
                destination = _save_mobile_stream(destination, storage.stream)
                saved_paths.append(destination)

            text = request.form.get("text", "")
            if text.strip():
                destination = _available_mobile_text_path(self.receive_dir)
                import io

                destination = _save_mobile_stream(destination, io.BytesIO(text.encode("utf-8")))
                saved_paths.append(destination)

            if not saved_paths:
                return {"message": "请选择文件或输入文字"}, 400
            if self._on_received is not None:
                self._on_received(saved_paths)
            return {"message": f"已保存 {len(saved_paths)} 个项目到电脑", "count": len(saved_paths)}

        self._server = make_server(self.host, self.port, app, threaded=True)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="LocalNetFTPMobileReceive",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def urls(self) -> list[ShareAddress]:
        return lan_download_urls(self.port, "LocalNetFTP", None)


def _share_files(paths: list[Path]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    counter = 1
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            files[str(counter)] = path
            counter += 1
            continue
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    files[str(counter)] = child
                    counter += 1
            continue
        raise ValueError(f"Unsupported share path: {path}")
    return files


def _mobile_share_item_html(file_id: str, path: Path) -> str:
    label = f"""<label class="file">
  <input type="checkbox" value="{escape(file_id)}">
  <span>{escape(path.name)}</span>
  <small>{escape(_format_file_size(path.stat().st_size))}</small>
</label>"""
    if _is_mobile_inline_image(path):
        return f"""{label}
<div class="preview">
  <img src="/preview/{escape(file_id)}" alt="{escape(path.name)}">
</div>"""
    if _is_mobile_inline_text(path):
        text = _read_mobile_text_preview(path)
        if text is not None:
            preview_id = f"text-preview-{file_id}"
            return f"""{label}
<div class="preview">
  <pre class="text-preview" id="{escape(preview_id)}">{escape(text)}</pre>
  <button type="button" data-copy-target="{escape(preview_id)}" onclick="copyText('{escape(preview_id)}')">复制文字</button>
</div>"""
    return label


def _is_mobile_inline_image(path: Path) -> bool:
    return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _is_mobile_inline_text(path: Path) -> bool:
    return path.suffix.lower() in {".txt", ".md", ".log", ".csv", ".json", ".py", ".ini", ".yaml", ".yml"}


def _read_mobile_text_preview(path: Path, max_bytes: int = 256 * 1024) -> str | None:
    import codecs

    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError:
        return None
    if len(data) > max_bytes:
        data = data[:max_bytes]
        truncated = True
    else:
        truncated = False
    for encoding in ("utf-8-sig", "gbk", "cp932"):
        try:
            text = codecs.getincrementaldecoder(encoding)().decode(data, final=not truncated)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    if truncated:
        return f"{text}\n..."
    return text


def _available_mobile_upload_path(receive_dir: Path, filename: str) -> Path:
    name = _safe_upload_filename(filename)
    return available_destination_path(receive_dir / name)


def _available_mobile_text_path(receive_dir: Path, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d%H%M%S%f")[:-3]
    return available_destination_path(receive_dir / f"手机文字_{timestamp}.txt", now)


def _safe_upload_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(normalized).name).strip().strip(".")
    name = name[:180].rstrip(" .")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    if not name:
        return "手机上传文件"
    return name


def _save_mobile_stream(destination: Path, stream) -> Path:
    """Reserve exclusively so concurrent uploads never overwrite each other."""
    while True:
        try:
            target = destination.open("xb")
            break
        except FileExistsError:
            destination = available_destination_path(destination)
    try:
        with target:
            shutil.copyfileobj(stream, target, length=1024 * 1024)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _format_file_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024


def _zip_arcname(path: Path, used_names: set[str]) -> str:
    base_name = path.name or "file"
    candidate = base_name
    counter = 2
    while candidate.casefold() in used_names:
        stem = path.stem or "file"
        suffix = path.suffix
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1
    used_names.add(candidate.casefold())
    return candidate


def _is_lan_address(address: str) -> bool:
    try:
        value = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    return not (value.is_loopback or value.is_link_local or value.is_unspecified or value.is_multicast)


def _windows_ipconfig_interfaces() -> list[tuple[str, str]]:
    try:
        output_bytes = subprocess.check_output(
            ["ipconfig"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=IPCONFIG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

    output = _decode_windows_command_output(output_bytes)
    interfaces: list[tuple[str, str]] = []
    current_name = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if raw_line and not raw_line.startswith(" ") and line.endswith(":"):
            current_name = _normalize_interface_name(line.rstrip(":"))
            continue
        if "IPv4" not in line:
            continue
        _, _, value = line.partition(":")
        match = re.match(r"\s*(\d{1,3}(?:\.\d{1,3}){3})(?:\s|\(|$)", value)
        address = match.group(1) if match else ""
        if address and _is_lan_address(address):
            interfaces.append((current_name or "局域网", address))

    return interfaces


def _decode_windows_command_output(output: bytes) -> str:
    encodings = [
        locale.getpreferredencoding(False),
        "mbcs",
        "utf-8",
        "gbk",
        "cp932",
    ]
    seen: set[str] = set()
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            return output.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return output.decode("utf-8", errors="replace")


def _normalize_interface_name(name: str) -> str:
    ethernet_prefix = "Ethernet adapter "
    if name.startswith(ethernet_prefix):
        suffix = name[len(ethernet_prefix) :].strip()
        if suffix.startswith("???"):
            suffix = suffix.replace("???", "", 1).strip()
            return f"以太网 {suffix}".strip()
        return f"以太网 {suffix}".strip()

    wireless_prefix = "Wireless LAN adapter "
    if name.startswith(wireless_prefix):
        suffix = name[len(wireless_prefix) :].strip()
        return f"无线网络 {suffix}".strip()

    return name
