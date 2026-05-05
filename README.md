# LocalNetFTP

LocalNetFTP is a Windows LAN file sharing tool planned in Python.

Current status: project scaffold only. Tray UI, LAN discovery, file transfer, resume support, and exe packaging will be implemented in later commits.

## Development

Install development dependencies:

```powershell
python -m pip install -r requirements-dev.txt
```

Run tests:

```powershell
python -m pytest
```

Run the placeholder app entry:

```powershell
python -m localnetftp
```

Build exe:

```powershell
python scripts/build_exe.py
```
