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

Build exe:

```powershell
python scripts/build_exe.py
```

The exe build uses Nuitka and writes `dist/LocalNetFTP.exe`.
