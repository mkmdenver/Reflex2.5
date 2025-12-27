
# Tools Folder

Independent utilities that are not long-running services:
- `symbol_manager`: manage symbol universe tiers and flags.
- `broker_db`: store broker account profiles and operational notes/stats.
- upcoming: `s3_downloader`.

Use `scripts/start_service.ps1 -Name symbols` to open the symbol manager CLI.
Use `python -m tools.broker_db.cli init` then `... set` to create broker profiles.
