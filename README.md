# gta-rpf-mcp

Windows stdio MCP server for staging GTA V story-mode mods in Agent Mod Manager (AMM). Starting with 1.1.0, the wheel includes a Python RPF7 reader and writer, the format tables, and the XML manifests for the GMM DLC template. An AMM job supplies an immutable archive baseline. The server edits its private copy and reopens the output for verification. AMM handles user approval, installation and rollback.

## Run with uvx

```powershell
uvx gta-rpf-mcp@latest
```

For an MCP client, select `stdio`, command `uvx`, and arguments `["gta-rpf-mcp@latest"]`. Python 3.11+ is required. The server uses the Python standard library and Windows CNG for AES. All runtime assets ship in the wheel. The original 205014 ZIP, PowerShell and .NET are unnecessary.

## MCP tools

Version 1.3.0 exposes the following operations through `tools/list`. Both AMM adapters start the same server and show the same catalog. The edition recorded in each job determines which operations it can run.

| Tool | Operation on the staged copy | Editions |
| --- | --- | --- |
| `gta_rpf_inspect_dlc` | Read `setup2.xml` and return the DLC pack name. | Legacy, Enhanced |
| `gta_rpf_register_dlc` | Register an existing DLC in `common/data/dlclist.xml`. | Legacy, Enhanced |
| `gta_rpf_replace_gameconfig` | Replace `common/data/gameconfig.xml`. | Legacy, Enhanced |
| `gta_rpf_build_vehicle_pack` | Build a GMM DLC containing the selected loose vehicle resources. | Legacy |
| `gta_rpf_build_ped_pack` | Build a GMM DLC containing the selected Ped resources and metadata. | Legacy |
| `gta_rpf_update_menyoo` | Rebuild the Menyoo vehicle list from the job's models. | Legacy |
| `gta_rpf_prepare` | Run any supported job, including multi-target rebuilds and empty uninstall jobs. | Legacy, Enhanced |
| `gta_rpf_status` | Report backend readiness, version and supported operations. | Legacy, Enhanced |
| `gta_rpf_describe_job` | Discover input paths, archive references and the preparation tools applicable to this job. | Legacy, Enhanced |
| `gta_rpf_list_entries` | List paths in a staged RPF7, including nested archives. | Supported RPF7 archives |
| `gta_rpf_read_entry` | Read an entry by path, with paging and a whole-entry digest. | Supported RPF7 entries |
| `gta_rpf_write_entry` | Replace an existing binary or supported resource file from a manifest input and verify the new archive copy. | Supported RPF7 files |
| `gta_rpf_add_entry` | Add a new file from a manifest input and verify its size and SHA-256 after reopening. | Supported RPF7 files |
| `gta_rpf_delete_entry` | Delete a file and verify that the reopened archive still has its parent and no longer has the file. | Supported RPF7 files |
| `gta_rpf_create_archive` | Build an OPEN RPF7 from named input files, including nested archive directories. | Supported RPF7 inputs |

Use the `mcpTool` and `jobId` returned by AMM's `prepare_gta_rpf_job`. Each of the six operation-specific preparation tools takes only `{"jobId":"…"}`. A matching tool stages every target in the job in one call. For example, a Legacy vehicle job may also rebuild the DLC registration and Menyoo list. Calling a tool whose operation is absent returns `tool_job_mismatch` before it consumes the job. After success, use the receipt for that job. Older AMM clients can continue to use `gta_rpf_prepare`.

`gta_rpf_status({})` returns `backend`, `version`, `ready` and separate `legacyJobs` and `enhancedJobs` lists. A mod still needs to match the edition and game build. For Enhanced DLC jobs, supply an existing Enhanced-compatible `dlc.rpf` and leave Menyoo off.

### Calling the archive tools

In AMM with the standalone archive tool, first call `prepare_gta_rpf_archive_job` with the selected `game_id` and the game-relative path to the outer `.rpf`. For `mods/update/update.rpf/common/data/dlclist.xml`, pass `archive_path: "mods/update/update.rpf"`. Use the returned `jobId` and `archive` with `gta_rpf_read_entry`, set `entry: "common/data/dlclist.xml"` and `encoding: "utf8"`. If the mods copy is absent, explicitly select `update/update.rpf` to inspect the stock archive. For an edit, supply the optional `source_paths` when requesting the job and use the corresponding returned `sourcePath` as the payload. Installation jobs also expose their declared inputs through the same archive tools.

1. Call `gta_rpf_describe_job({"jobId": …})`. An archive input includes `archive: {path, name}` with its canonical basename for decryption. Other manifest inputs have a `path` usable as `sourcePath`. Use the returned `nextOffset` to page through inputs.
2. Pass the returned `archive` to `gta_rpf_list_entries({jobId, archive})`. Call `gta_rpf_read_entry({jobId, archive, entry: "common/data/dlclist.xml", encoding: "utf8"})` to read the DLC list as text. The default encoding is `base64`. A read returns the entry size, SHA-256, encoding, data and next byte offset.
3. Call `gta_rpf_write_entry({jobId, archive, entry, sourcePath})` to replace an existing file, `gta_rpf_add_entry({jobId, archive, entry, sourcePath})` to add a new file, or `gta_rpf_delete_entry({jobId, archive, entry})` to remove one. These tools accept paths at the root, in ordinary directories and through multiple existing nested archives, such as `x64/packs.rpf/content/inner.rpf/common/data/dlclist.xml`. Paths are case-insensitive. Add creates missing ordinary directories and returns `entry_exists` for an occupied path. Read, write and delete return `entry_not_found` for a missing file. Add and write accept zero-byte ordinary files from a standalone archive job.
4. To build an archive, call `gta_rpf_create_archive({jobId, archiveName: "dlc.rpf", entries: [{entry: "setup2.xml", sourcePath}]})`. Paths such as `x64/models.rpf/example.xml` create a nested archive during creation. Pass the `archive` returned by any mutation to the next archive tool.

Archive mutations produce a private copy in the job workspace. The input archive stays available at its original reference. The file tools work inside existing nested archives, and deletion preserves ordinary directories and nested archive containers. Use the operation-specific preparation tool to stage an installation for AMM review.

Legacy and Enhanced jobs both support file edits for compatible PC RPF7 data, including XML such as `common/data/dlclist.xml`. Resource reads, adds and writes require a supported RSC7 version. Enhanced loose model installation and Menyoo remain outside the supported preparation job types.

The RPF7 layout and encryption follow [Cryma/gta-toolkit](https://github.com/Cryma/gta-toolkit). The read, write, create and recursive listing operations follow [GlossMod/gmm-plug-in/rpf](https://github.com/GlossMod/gmm-plug-in/tree/main/rpf). Revisions, license notices and the template provenance are in `THIRD-PARTY-NOTICES.txt`.

## Job protocol (schema 1)

`gta_rpf_prepare({"jobId":"0123456789abcdef0123456789abcdef"})` uses exactly `%LOCALAPPDATA%\GlossMod\AMM\gta-rpf-jobs\<jobId>`. The caller creates `input.json` and copies all required bytes inside the task directory first. Task paths are relative; absolute paths, traversal, ADS, reserved names, symlinks, junctions, other reparse points and hard links are rejected. All references are size- and SHA-256-verified against securely opened copies. AMM installation preparation can be claimed once per job. No third-party task can request a DLL path, script, archive entry, output path, or native command.

Standalone file jobs use `{schemaVersion: 1, jobId, kind, archiveInputs: [{name, source: FileRef}], payloadInputs: [FileRef, ...]}`. The basename in `name` identifies the snapshot for NG decryption. `gta_rpf_describe_job` lists the archive and its payloads and returns an empty `preparationTools` list. File operations can be chained in the same job using the returned archive reference.

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

Set the version in `src/gta_rpf_mcp/__init__.py`, commit it to `main`, and publish a GitHub Release with the matching tag, such as `v1.3.0`. The workflow builds the sdist and wheel, installs and tests the wheel on Windows, then publishes both tested distributions to PyPI. If the first run reaches PyPI before authorization is configured, configure the publisher and rerun the failed job for that release.
