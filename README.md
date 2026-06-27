# WithoutBG — GIMP Plugin

A GIMP 3.0 plugin that removes image backgrounds using your local
[WithoutBG](http://127.0.0.1:8000) server.  
All processing happens on-device — nothing leaves the machine.

## Requirements

| Dependency | Notes |
|---|---|
| GIMP 3.0 | Available at [gimp.org](https://www.gimp.org) |
| WithoutBG server | Must be running at `http://127.0.0.1:8000` — Docker `service-cpu` / `service-gpu`, or the Mac server app (or change `SERVER_URL` in `withoutbg/withoutbg.py`) |

### Start a server

**Docker (CPU or GPU, any platform):**

```bash
docker run --rm -p 8000:8000 withoutbg/withoutbg-openweights-v3-service-cpu:latest
```

**Mac server app:** start it from the menu bar (also defaults to port 8000).

Run one backend at a time on port 8000.

## Install

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/withoutbg/withoutbg-gimp/main/install.sh)"
```

Or, if you already have the repo:

```bash
./install.sh
```

Then restart GIMP. The plugin appears under:

```
Tools ▸ WithoutBG ▸ Remove Background…
```

## Usage

1. Open any image in GIMP.
2. Go to **Tools ▸ WithoutBG ▸ Remove Background…**
3. The dialog shows the server status and lets you override the server URL.
4. Click **Remove Background**.

The plugin adds an unapplied layer mask from the server’s alpha matte. Review it, then use **Layer ▸ Mask ▸ Apply Layer Mask** to commit.

## Project layout

```
withoutbg-gimp/
├── install.sh              # one-shot install script
├── README.md
└── withoutbg/
    └── withoutbg.py        # the plug-in (also symlinked into GIMP's plug-ins dir)
```

## Configuration

The default server URL (`http://127.0.0.1:8000`) is hardcoded at the top of
`withoutbg/withoutbg.py`.  You can also change it per-session inside the
interactive dialog — GIMP remembers the last-used value between runs.

## Uninstall

```bash
rm -rf ~/Library/Application\ Support/GIMP/3.0/plug-ins/withoutbg   # macOS
# or
rm -rf ~/.config/GIMP/3.0/plug-ins/withoutbg                        # Linux
```
