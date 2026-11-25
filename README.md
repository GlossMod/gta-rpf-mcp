# gta-rpf-mcp

Opt-in Windows stdio MCP server for Agent Mod Manager. Prepares local GTA V **Legacy** archive jobs; it does not install mods or read the game directory. The native desktop manager chooses the immutable baseline and mod inputs, verifies the result, and handles review, deployment and rollback. Enhanced fails closed; no OIV, arbitrary RPF editing/assembly invocation, or online installation is exposed.

## Run with uvx

On Windows with [uv](https://docs.astral.sh/uv/) installed, start the PyPI release with:

```powershell
uvx gta-rpf-mcp@latest
```

The process speaks MCP over standard input/output and waits for a client. In a client configuration use `stdio`, command `uvx`, and arguments `["gta-rpf-mcp@1.0.0"]` to select this release. To run directly from its GitHub tag with Git installed:

```powershell
uvx --from git+https://github.com/GlossMod/gta-rpf-mcp.git@v1.0.0 gta-rpf-mcp
```

## Activation and the local prerequisite

Build with `uv build --wheel --out-dir dist` from this directory. The package has no runtime Python dependencies. Run the installed `gta-rpf-mcp` console script (Python 3.11+, Windows PowerShell 5.1 and .NET Framework 4.8). No setup is performed by launching the server. The separate prerequisite is a user-/manager-provisioned local ZIP at:

```
%LOCALAPPDATA%\GlossMod\AMM\gta-rpf-tools\205014-356110.zip
```

Only 3DM project **205014**, resource **356110** (version **1.4.0**) is supported. The original ZIP must match the build's embedded SHA‑256; an extracted folder, user-provided DLL, modified ZIP, and alternate release are not accepted. The SHA‑256 pins bytes independently acquired from the 3DM first-party HTTPS host and rechecked locally; it is **not** a publisher signature. The service never downloads, registers, or runs an installer. The wheel redistributes no third-party DLLs, game key files or GMM template bytes. The local ZIP is read only; only the three fixed runtime DLLs, six required key files and allowlisted template data are copied to isolated per-job staging. The template is always restored from the ZIP for each job. The service runs the approved assemblies as the current user, not in an OS security sandbox. Its Windows Job Object sets process and memory limits; individual bridge requests also time out.

`gta_rpf_status({})` is read only. It checks the pinned ZIP, the GMM template layout and the availability of Windows PowerShell and .NET Framework; it does not run the release, and it does not claim game-runtime compatibility.

## Job protocol (schema 1)

`gta_rpf_prepare({"jobId":"0123456789abcdef0123456789abcdef"})` uses exactly `%LOCALAPPDATA%\GlossMod\AMM\gta-rpf-jobs\<jobId>`. The caller creates `input.json` and copies all required bytes inside the task directory first. Task paths are relative; absolute paths, traversal, ADS, reserved names, symlinks, junctions, other reparse points and hard links are rejected. All references are size- and SHA-256-verified against securely opened copies. Jobs are one shot; interrupted or failed jobs should be replaced by fresh IDs. No third-party task can request a DLL path, script, archive entry, output path, or native command.

A `FileRef` is `{"path":"inputs/<sha256>","sha256":"<64 lowercase hex>","size":123}`. A normal `input.json` is `{"schemaVersion":1,"jobId":"...","kind":"legacy","targets":[...]}`; targets are unique and each has `id`, `baseline` (`FileRef` or `null`), and `contributions`. Contributions describe the **entire active set for that target**, in manager order:

| Target | Baseline | Contribution objects | Fixed artifact |
| --- | --- | --- | --- |
| `update` | selected immutable `update.rpf` snapshot, mandatory | `dlc`, `gameconfig`, `vehicle`, `ped` | `output/update.rpf` |
| `gmm` | `null`: use pinned stock template | `vehicle`, `ped` | `output/gmm.rpf` (install name `dlc.rpf`) |
| `menyoo` | previous unmanaged XML snapshot, or `null` | `dlc`, `vehicle` | `output/menyoo.xml` |

* DLC: `{"kind":"dlc","packName":"example","source":FileRef}`. The service verifies `setup2.xml` `nameHash`, and changes only the `dlclist.xml` entry list. Rust deploys the supplied DLC archive separately.
* Gameconfig: `{"kind":"gameconfig","source":FileRef}`. Only `common/data/gameconfig.xml` in the selected copy is replaced. Multiple distinct replacements fail.
* Vehicle: `{"kind":"vehicle","files":[{"name":"example.yft","source":FileRef},...]}`.
* Ped: `{"kind":"ped","modelName":"example","sex":"Female","streamed":true,"files":[{"name":"example.ymt","source":FileRef},{"name":"example/hand_000_u.ydd","source":FileRef}]}`. Only known GTA resource extensions are accepted; streamed components must belong to the named model. The service creates the fixed Ped metadata from the selected sex/streaming options.

The manager restores the selected baseline or removes its installed output when a target loses its last contributor. An empty `targets` list is valid, with an empty output directory. `inspectDlc:FileRef` is an optional, **mutually exclusive** operation with non-empty targets: it probes the RPF7 copy, produces no artifact and returns `{"schemaVersion":1,"jobId":"...","status":"inspected","packName":"example"}`. Official tooling reads under canonical NG archive names; `gmm.rpf` is only a transport filename.

On success, `result.json` is published after output, with `{"schemaVersion":1,"jobId":"...","status":"ok","outputs":[{"id":"update","sha256":"...","size":123}]}`. Failures normally produce `status:"error"`, `code` and a pathless `message`; a rejected/used workspace or interrupted publish need not get a result. An output without a matching successful result is not authoritative. Readback verifies edited XML, the produced GMM inventory and metadata, and artifact hashes; it does not certify that third-party binary models render or work in-game. New Menyoo rows come from a matching base YFT/YTD pair and use the model name as their label.

## Verification

```
$env:PYTHONPATH = 'src'
py -3.11 -m unittest discover -s tests -v
uv build --wheel --out-dir dist
```

Tests use disposable job trees and synthetic content. They do not inspect the game or current real manager job directory. To also run the Windows PowerShell integration tests against the pinned release, set `GTA_RPF_TEST_RELEASE_ZIP` to an already verified ZIP and run the same tests. That exercises a synthetic OPEN RPF7 baseline, a test DLC, GMM generation from the real template, streamed and non-streamed Ped metadata, and Menyoo extraction. Nothing is downloaded or installed. It does not test retail game archives or model rendering.

## Publishing a release

Configure a PyPI Trusted Publisher for project `gta-rpf-mcp`, owner `GlossMod`, repository `gta-rpf-mcp`, workflow `publish.yml`, environment `pypi`. For the first release, use [PyPI pending publishers](https://pypi.org/manage/account/publishing/). The publisher exchanges GitHub's release-job identity for a short-lived PyPI credential.

Set the version in `src/gta_rpf_mcp/__init__.py`, commit it to `main`, and publish a GitHub Release with the matching tag, such as `v1.0.0`. The workflow builds the sdist and wheel, installs and tests the wheel on Windows, then publishes both tested distributions to PyPI. If the first run reaches PyPI before authorization is configured, configure the publisher and rerun the failed job for that release.
