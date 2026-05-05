# LocalNetFTP

LocalNetFTP is a Windows LAN file sharing tool planned in Python.

Current status: tray UI, LAN discovery, file transfer with resume and checksum verification, clipboard payload sending, share mode, and Nuitka exe packaging are implemented.

## Development

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run tests:

```powershell
python -m pytest
```

Run the tray app:

```powershell
python -m localnetftp
```

Start a foreground debug client:

```powershell
python scripts/start_debug_client.py
```

To use a named isolated instance:

```powershell
python scripts/start_debug_client.py A
```

Verify core transfer on one machine:

```powershell
python scripts/verify_local_transfer.py
```

This starts two loopback receivers with different ports, sends files both ways through the real transfer client/server path, and cleans up its temporary files when done.

Start two tray instances on one machine for UI testing:

```powershell
python scripts/start_dev_pair.py
```

You can also start them manually:

```powershell
python -m localnetftp --dev-instance A
python -m localnetftp --dev-instance B
```

Development instances use isolated config folders, different receive ports, and a local peer registry so the two tray windows can discover each other as `127.0.0.1` peers.

Internet transfer uses the Python `iroh` package directly. Select `局域网外用户（生成 ticket）` in the floating window, drop a file or folder, then share the generated ticket. The receiver can right-click the tray icon and choose `输入 ticket 接收文件`.

Build exe:

```powershell
python scripts/build_exe.py
```

The exe build uses Nuitka and writes `dist/LocalNetFTP.exe`.
