# gta-rpf-mcp

Windows stdio MCP server for staging GTA V story-mode mods in Agent Mod Manager (AMM). Starting with 1.1.0, the wheel includes a Python RPF7 reader and writer, the format tables, and the XML manifests for the GMM DLC template. An AMM job supplies an immutable archive baseline. The server edits its private copy and reopens the output for verification. AMM handles user approval, installation and rollback.

## Run with uvx

```powershell
uvx gta-rpf-mcp@latest
```

For an MCP client, select `stdio`, command `uvx`, and arguments `["gta-rpf-mcp@latest"]`. Python 3.11+ is required. The server uses the Python standard library and Windows CNG for AES. All runtime assets ship in the wheel. The original 205014 ZIP, PowerShell and .NET are unnecessary.

| AMM operation | Legacy | Enhanced |
| --- | --- | --- |
| Read `setup2.xml` to identify an existing DLC | Yes | Yes |
| Register a DLC in `common/data/dlclist.xml` | Yes | Yes |
| Replace `common/data/gameconfig.xml` | Yes | Yes |
| Build a GMM DLC from loose vehicle or Ped resources | Yes, Legacy RSC7 | Unverified |
| Generate Menyoo model list entries | Yes | Unverified |

`gta_rpf_status({})` returns `backend`, `version`, `ready` and separate `legacyJobs` and `enhancedJobs` lists. A mod still needs to match the edition and game build. For Enhanced DLC jobs, supply an existing Enhanced-compatible `dlc.rpf` and leave Menyoo off.

The RPF7 layout and encryption follow [Cryma/gta-toolkit](https://github.com/Cryma/gta-toolkit). The read, write, create and recursive listing operations follow [GlossMod/gmm-plug-in/rpf](https://github.com/GlossMod/gmm-plug-in/tree/main/rpf). Revisions, license notices and the template provenance are in `THIRD-PARTY-NOTICES.txt`.

## Job protocol (schema 1)

`gta_rpf_prepare({"jobId":"0123456789abcdef0123456789abcdef"})` uses exactly `%LOCALAPPDATA%\GlossMod\AMM\gta-rpf-jobs\<jobId>`. The caller creates `input.json` and copies all required bytes inside the task directory first. Task paths are relative; absolute paths, traversal, ADS, reserved names, symlinks, junctions, other reparse points and hard links are rejected. All references are size- and SHA-256-verified against securely opened copies. Jobs are one shot; interrupted or failed jobs should be replaced by fresh IDs. No third-party task can request a DLL path, script, archive entry, output path, or native command.

A `FileRef` is `{"path":"inputs/<sha256>","sha256":"<64 lowercase hex>","size":123}`. A normal `input.json` is `{"schemaVersion":1,"jobId":"...","kind":"legacy","targets":[...]}`; targets are unique and each has `id`, `baseline` (`FileRef` or `null`), and `contributions`. Contributions describe the **entire active set for that target**, in manager order:

| Target | Baseline | Contribution objects | Fixed artifact |
| --- | --- | --- | --- |
| `update` | selected immutable `update.rpf` snapshot, mandatory | `dlc`, `gameconfig`, `vehicle`, `ped` | `output/update.rpf` |
| `gmm` | `null`: use the bundled Legacy template | `vehicle`, `ped` | `output/gmm.rpf` (install name `dlc.rpf`) |
| `menyoo` | previous unmanaged XML snapshot, or `null` | `dlc`, `vehicle` | `output/menyoo.xml` |

* DLC: `{"kind":"dlc","packName":"example","source":FileRef}`. The service verifies `setup2.xml` `nameHash`, and changes only the `dlclist.xml` entry list. Rust deploys the supplied DLC archive separately.
* Gameconfig: `{"kind":"gameconfig","source":FileRef}`. Only `common/data/gameconfig.xml` in the selected copy is replaced. Multiple distinct replacements fail.
* Vehicle: `{"kind":"vehicle","files":[{"name":"example.yft","source":FileRef},...]}`.
* Ped: `{"kind":"ped","modelName":"example","sex":"Female","streamed":true,"files":[{"name":"example.ymt","source":FileRef},{"name":"example/hand_000_u.ydd","source":FileRef}]}`. Only known GTA resource extensions are accepted; streamed components must belong to the named model. The service creates the fixed Ped metadata from the selected sex/streaming options.

The manager restores the selected baseline or removes its installed output when a target loses its last contributor. An empty `targets` list is valid, with an empty output directory. `inspectDlc:FileRef` is an optional, **mutually exclusive** operation with non-empty targets: it probes the RPF7 copy, produces no artifact and returns `{"schemaVersion":1,"jobId":"...","status":"inspected","packName":"example"}`. NG directory encryption depends on the archive basename and length, so staged files use the canonical archive name; `gmm.rpf` is only a transport filename.

On success, `result.json` is published after output, with `{"schemaVersion":1,"jobId":"...","status":"ok","outputs":[{"id":"update","sha256":"...","size":123}]}`. Failures normally produce `status:"error"`, `code` and a pathless `message`; a rejected/used workspace or interrupted publish need not get a result. An output without a matching successful result is not authoritative. Readback verifies edited XML, the produced GMM inventory and metadata, and artifact hashes; it does not certify that third-party binary models render or work in-game. New Menyoo rows come from a matching base YFT/YTD pair and use the model name as their label.

## Verification

```
$env:PYTHONPATH = 'src'
py -3.11 -m unittest discover -s tests -v
uv build --wheel --out-dir dist
```

Windows acceptance also exercised full copies of the retail Legacy 1.0.3889.0 and Enhanced 1.0.1158.16 archives. The 205014 C# reader independently reopened the resulting `update.rpf` and `dlc.rpf` files and matched Python for gameconfig, dlclist and setup2 XML. A Legacy GMM job using `jester2` and `u_m_y_smugmech_01` round-tripped six model files byte-for-byte. In-game loading was not tested.

The automatic suite uses disposable jobs, independent RPF7 fixtures and real readback of generated archives. To run the same checks against a wheel, install that wheel into a clean environment and leave `PYTHONPATH` unset. See the edition table above for the operations enabled by AMM.

## Publishing a release

Configure a PyPI Trusted Publisher for project `gta-rpf-mcp`, owner `GlossMod`, repository `gta-rpf-mcp`, workflow `publish.yml`, environment `pypi`. For the first release, use [PyPI pending publishers](https://pypi.org/manage/account/publishing/). The publisher exchanges GitHub's release-job identity for a short-lived PyPI credential.

Set the version in `src/gta_rpf_mcp/__init__.py`, commit it to `main`, and publish a GitHub Release with the matching tag, such as `v1.1.0`. The workflow builds the sdist and wheel, installs and tests the wheel on Windows, then publishes both tested distributions to PyPI. If the first run reaches PyPI before authorization is configured, configure the publisher and rerun the failed job for that release.
