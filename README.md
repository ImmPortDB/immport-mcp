# ImmPort MCP Server

An [MCP](https://modelcontextprotocol.io) server for [ImmPort](https://www.immport.org), the NIAID immunology data repository. It lets AI assistants such as Claude search ImmPort studies and subjects, read study data and assay results, look up controlled vocabularies and the data model, and find study files.

**Using claude.ai?** You don't need to install anything. Add the hosted connector `https://mcp.immport.org` in claude.ai (Settings → Connectors) and sign in with your ImmPort account.

This package is for running the server yourself, on your own computer: in Claude Desktop, Claude Code, or any other MCP client that starts servers from the command line.

## 1. Get an ImmPort API key

1. Sign in at https://www.immport.org. Registration is free.
2. Open https://www.immport.org/auth/api/keys and create a key.
3. Download the JSON and copy the value of `api_key`.

Keys are valid for **30 days**. When a key expires, create a new one and update your configuration. Searching and controlled-vocabulary lookups work without a key; study data, assay results and files need one.

## 2. Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (recommended) or Python 3.11+.

```bash
uvx immport-mcp            # runs the latest version, no separate install step
# or
pip install immport-mcp    # then run: immport-mcp
```

The server talks to its client over stdio, so you normally don't run it yourself. Your MCP client starts it.

## 3. Connect your client

**Claude Code**
```bash
claude mcp add immport -e IMMPORT_API_KEY=<your key> -- uvx immport-mcp
```

**Claude Desktop**: add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), then restart Claude Desktop:
```json
{
  "mcpServers": {
    "immport": {
      "command": "uvx",
      "args": ["immport-mcp"],
      "env": { "IMMPORT_API_KEY": "<your key>" }
    }
  }
}
```

**Other MCP clients**: configure a stdio server with the command `uvx immport-mcp` and the environment variable `IMMPORT_API_KEY`.

## What you can ask

- *"Find influenza vaccine studies in older adults and compare their size and available assays."*
- *"Compare SDY180, SDY212 and SDY269: arms, demographics and which assay data each has."*
- *"When was SDY180 released, and what changed in its latest update?"*
- *"I downloaded SDY269's Tab.zip. Which tables do I join for each subject's arm, age and HAI titers? Write the SQL."*
- *"List the Tab.zip and MySQL.zip files for SDY180."*

## Tools

| Tool | What it does | Needs API key |
|---|---|---|
| `search_studies` | Search studies by condition, assay, species, sponsor and more | No |
| `search_subjects` | Search subjects across studies (demographics, arms, exposures) | No |
| `lookup_controlled_vocabulary` | Valid values for filters (conditions, assay types, …) | No |
| `get_data_model` | Tables of the ImmPort database: columns, keys, joins | No |
| `get_study_data` | Any study sub-resource (summary, arms, demographics, …) for up to 20 studies at once | Yes |
| `get_study_data_availability` | Record counts per data category, to see what a study contains | Yes |
| `get_assay_results` | Assay results (ELISA, HAI, flow cytometry, …) | Yes |
| `get_study_files` | List a study's files, stream text files, or get download links | Yes |
| `get_release_notes` | Data releases and a study's release history | Yes |

**Resources:** `immport://data-model`, `immport://query-tips`, `immport://search-fields`, `immport://assay-results-reference`, `immport://api-schema`.

**Prompts:** guided workflows to discover studies for a topic, analyse one study in depth, compare immune responses across studies, build a cross-study cohort, and find and download files.

## Configuration

Set these as environment variables, or in a `.env` file in the directory the server is started from.

| Variable | Default | Meaning |
|---|---|---|
| `IMMPORT_API_KEY` | (none) | Your ImmPort API key (see above) |
| `IMMPORT_METRICS_ENABLED` | `true` | Send usage metrics (see below). `false` sends nothing |
| `IMMPORT_BASE_URL` | `https://immport.org/data/query` | ImmPort Data Query API |
| `RESPONSE_CACHE_TTL` | `300` | Seconds to cache API responses (`0` turns caching off) |
| `MCP_LOG_LEVEL` | `WARNING` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## Usage metrics

To understand how ImmPort data is used, the server sends ImmPort one usage record for each tool call. The record holds the tool name, its parameters (e.g. study accessions or search terms), and your ImmPort username. It's sent with your API key, the same way ImmPort's own website and API record usage. No other data from your computer or conversation is sent.

To turn it off, set `IMMPORT_METRICS_ENABLED=false`.

## Support

- ImmPort help: https://www.immport.org (Contact Us)
- Bugs and feature requests: https://github.com/ImmPortDB/immport-mcp/issues
- Claude skills for ImmPort: https://github.com/ImmPortDB/immport-skills

This repository is published from ImmPort's internal repository on each release; see [CONTRIBUTING.md](CONTRIBUTING.md).
