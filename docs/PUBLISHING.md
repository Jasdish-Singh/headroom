# Publishing Headroom

This is the checklist. Do not run a publish command until the version you want the public to install is the version in `pyproject.toml`, `server.json`, and the winget manifests. The release workflow builds an exe and can sign it. It does not upload to PyPI, winget, or any MCP directory.

GitHub owner: `Jasdish-Singh`. Repository: `https://github.com/Jasdish-Singh/headroom`. PyPI project: `headroom-pc`. MCP registry name: `io.github.Jasdish-Singh/headroom`.

## 1. PyPI (`headroom-pc`)

The official MCP Registry checks the PyPI README for the ownership string, so publish the package before `mcp-publisher publish`.

1. Confirm `pyproject.toml` has `name = "headroom-pc"` and the version you are shipping.
2. Confirm `README.md` contains this exact line, which the registry compares to `server.json`:

   ```text
   mcp-name: io.github.Jasdish-Singh/headroom
   ```

3. Build and upload from a clean tree:

   ```powershell
   py -3.12 -m pip install build twine
   py -3.12 -m build
   py -3.12 -m twine upload dist/*
   ```

4. Open `https://pypi.org/project/headroom-pc/` and confirm the description shows the `mcp-name` line. The registry reads the package description PyPI stored from this README.

The console script stays `headroom`. `headroom-pc` is an alias of the same entry point so `uvx headroom-pc mcp` starts the server.

## 2. Official MCP Registry (`mcp-publisher`)

Metadata is `server.json` at the repository root. The schema is `https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`.

| Field | Value |
| --- | --- |
| `name` | `io.github.Jasdish-Singh/headroom` |
| `packages[0].registryType` | `pypi` |
| `packages[0].identifier` | `headroom-pc` |
| `packages[0].transport.type` | `stdio` |
| `packages[0].packageArguments` | one positional argument, `mcp` |
| `version` | the same string as the PyPI version |

`description` must be 100 characters or fewer. The registry rejects a longer one.

Steps:

1. Install `mcp-publisher` from `https://github.com/modelcontextprotocol/registry/releases`. On Windows, take `mcp-publisher_windows_amd64.tar.gz` (or the current asset name on that page) and put `mcp-publisher.exe` on `PATH`.
2. From the repository root, check the file you are about to send:

   ```powershell
   mcp-publisher publish --help
   ```

   Publishing itself is the next command, without `--help`. Do not run it until PyPI already has this version.
3. Authenticate as the GitHub user `Jasdish-Singh`. That account owns the `io.github.Jasdish-Singh/*` namespace.

   ```powershell
   mcp-publisher login github
   ```

   The CLI prints a device code. Open `https://github.com/login/device`, enter the code, and authorize. A GitHub Actions publish uses `mcp-publisher login github-oidc` with `permissions: id-token: write` instead of the device flow. This repo's release workflow does not do that.
4. Publish the listing:

   ```powershell
   mcp-publisher publish
   ```

   The CLI validates `server.json`, and the registry checks the GitHub namespace plus the `mcp-name` line on the PyPI description. The public page is `https://registry.modelcontextprotocol.io/`.

To ship a new version, bump `version` in `pyproject.toml` and in `server.json` together, upload that version to PyPI, then run `mcp-publisher publish` again.

## 3. Smithery

`smithery.yaml` is a stdio server with no config. The start command is `headroom` with argument `mcp`.

1. Push `smithery.yaml` to the default branch of `Jasdish-Singh/headroom`.
2. Open `https://smithery.ai/new` (the project config reference is `https://smithery.ai/docs/build/project-config`).
3. Connect the GitHub repository. Smithery reads `smithery.yaml` and scans the tools.
4. Confirm the scanned command is `headroom mcp` and that `clean`, `move`, and `startup_disable` are described as plans.

Smithery needs `headroom` on the PATH of the machine that launches it, or a Smithery runtime that installed `headroom-pc`. The Windows desktop configs in the README use `headroom.exe mcp` instead, because those clients run on the PC where Headroom is installed.

## 4. Glama

`glama.json` names the maintainer `Jasdish-Singh`.

1. Push `glama.json` to the default branch.
2. Open `https://glama.ai/mcp/servers` and add the server with the repository URL `https://github.com/Jasdish-Singh/headroom`.
3. Claim ownership. Sign in to Glama with GitHub as `Jasdish-Singh`. For a personal repo, GitHub login can attach the listing. The `glama.json` file is what an organization repo uses, and it is the file to re-claim after the maintainer list changes.
4. If the claim does not stick, check that `maintainers` is exactly the GitHub username and that the JSON parses.

## 5. mcp.so

1. Open `https://mcp.so/submit`.
2. Submit `https://github.com/Jasdish-Singh/headroom` as an MCP server.
3. Review the name, description, and the stdio config (`headroom` / `headroom.exe` with argument `mcp`) before confirming.
4. Free submissions wait for review. mcp.so also offers a paid publish that skips the queue. Either way, the listing should say that cleanup, move, and startup disable return plans.

## 6. PulseMCP

1. Open `https://www.pulsemcp.com/submit`.
2. In October 2026 that form said it was not accepting new servers or edits, and it told authors to publish to the official MCP Registry instead. PulseMCP ingests registry entries when submissions reopen. Re-read the page before assuming it is still paused.
3. If the form is open, submit `https://github.com/Jasdish-Singh/headroom`.
4. If the form is paused, `mcp-publisher publish` (section 2) is the step that gets Headroom into the queue PulseMCP reads later.

## 7. Code signing

Sign `headroom.exe` and `Headroom-Setup.exe` before they are attached to a GitHub Release or pointed at by winget. Pick one signer. The release workflow runs Azure Trusted Signing when `AZURE_CLIENT_ID` is set, and SignPath only when that Azure secret is empty and `SIGNPATH_API_TOKEN` is set. With neither secret, the job uploads an unsigned exe and still succeeds.

The workflow file is `.github/workflows/release.yml`. It starts only from **Run workflow** (`workflow_dispatch`). It is not on push, and it has no PyPI, winget, or `mcp-publisher` step.

### Azure Trusted Signing

Azure Artifact Signing (Trusted Signing) uses a certificate in your tenant. The GitHub job authenticates with OpenID Connect via `azure/login@v3`, then signs with `azure/artifact-signing-action@v2`.

Create the signing account and certificate profile in Azure, then a federated credential for this repository. Repository **secrets**:

| Secret | Use |
| --- | --- |
| `AZURE_CLIENT_ID` | App registration used by `azure/login` |
| `AZURE_TENANT_ID` | Tenant |
| `AZURE_SUBSCRIPTION_ID` | Subscription |

Repository **variables**:

| Variable | Use |
| --- | --- |
| `AZURE_SIGNING_ACCOUNT` | `signing-account-name` |
| `AZURE_CERTIFICATE_PROFILE` | `certificate-profile-name` |
| `AZURE_SIGNING_ENDPOINT` | Regional endpoint. Empty uses `https://eus.codesigning.azure.net/` |

The workflow needs `id-token: write`, which `release.yml` already sets. The sign step is skipped when `AZURE_CLIENT_ID` is unset, because the step condition is `env.AZURE_CLIENT_ID != ''`.

### SignPath (free for eligible open source)

SignPath Foundation signs open-source projects that meet its conditions: an OSI license with no dual-license, no proprietary code in the signed package, a maintained project, and a public description of what the binary does. Apply at `https://signpath.org/`. The certificate is issued to SignPath Foundation, not to you.

After the org, project, and signing policy exist, and the SignPath GitHub App can see this repo:

| Secret | Use |
| --- | --- |
| `SIGNPATH_API_TOKEN` | Submitter token. Gates the SignPath steps. |

| Variable | Use |
| --- | --- |
| `SIGNPATH_ORGANIZATION_ID` | `organization-id` |
| `SIGNPATH_PROJECT_SLUG` | `project-slug` |
| `SIGNPATH_POLICY_SLUG` | `signing-policy-slug` |

The workflow uploads `dist/headroom.exe` with `actions/upload-artifact@v4`, then `signpath/github-action-submit-signing-request@v3` waits and writes the signed file back to `dist`. SignPath's artifact configuration for that policy must accept the zip that `upload-artifact` produces. Those steps are skipped when Azure signing is configured, and also skipped when `SIGNPATH_API_TOKEN` is unset.

### Where the signed file goes

`release.yml` always ends by uploading the `dist/headroom.exe` artifact named `headroom.exe`. If a signer ran, that file is the signed one. Download it from the workflow run. Building the installer is still local, with Inno Setup:

```powershell
iscc packaging\headroom.iss
```

Sign `Headroom-Setup.exe` with the same account before you publish it. The GitHub workflow does not compile the installer.

## 8. GitHub Release and winget

1. Run the release workflow from the Actions tab. Download `headroom.exe`. Build and sign `Headroom-Setup.exe`.
2. Create a GitHub Release on `Jasdish-Singh/headroom` and attach both files. Creating the release is a separate manual step. The workflow will not do it.
3. Replace the placeholder `InstallerUrl` and `InstallerSha256` in `packaging/winget/JasdishSingh.Headroom.installer.yaml`. The URL should be the release asset. The hash is SHA256 of the installer bytes.
4. Submit the three manifests under `packaging/winget/` to `microsoft/winget-pkgs` as `JasdishSingh.Headroom`. Do that in a pull request on that repository, not from this workflow.

## Order

1. Tests on `main`.
2. PyPI `headroom-pc` for that version, with the `mcp-name` line in the description.
3. Optional: sign the exe (release workflow) and publish a GitHub Release, then winget.
4. `mcp-publisher login github`, then `mcp-publisher publish`.
5. Smithery, Glama, and mcp.so from the URLs above.
6. PulseMCP when its submit form is open, or by way of the registry entry.
