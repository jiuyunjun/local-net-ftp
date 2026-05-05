# LocalNetFTP

LocalNetFTP is a Windows LAN file sharing tool planned in Python.

Current status: tray UI, LAN discovery, file transfer, clipboard payload sending, share mode, and Nuitka exe packaging are implemented. Resume support is still planned.

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

Build exe:

```powershell
python scripts/build_exe.py
```

The exe build uses Nuitka and writes `dist/LocalNetFTP.exe`.
