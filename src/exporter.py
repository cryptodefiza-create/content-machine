"""Export drafts for review workflows (CSV + Google Sheets)."""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .utils import get_project_root, logger


@dataclass
class ExportSettings:
    enabled: bool = True
    export_dir: str = "data/exports"
    format: str = "csv"  # csv | gsheets | both
    master_csv: bool = True
    master_csv_path: str = "data/exports/all_runs.csv"


class CsvExporter:

    def __init__(self, settings: ExportSettings):
        self.settings = settings

    def export_run(self, run_id: str, topic: dict, per_persona: Dict[str, object], status: str = "pending"):
        if not self.settings.enabled:
            return

        root = get_project_root()
        export_dir = root / self.settings.export_dir
        export_dir.mkdir(parents=True, exist_ok=True)
        path = export_dir / f"run_{run_id}.csv"

        rows = self._build_rows(run_id, topic, per_persona, status=status)
        if not rows:
            return

        self._write_csv(path, rows, overwrite=True)

        logger.info(f"Run {run_id}: exported CSV to {path}")
        if self.settings.master_csv:
            self._append_master(rows)

    def _build_rows(self, run_id: str, topic: dict, per_persona: Dict[str, object], status: str = "pending") -> List[dict]:
        rows: List[dict] = []
        created_at = datetime.now(timezone.utc).isoformat()

        for persona_key, draft in per_persona.items():
            content = draft.content
            parts = draft.thread_parts or []
            is_thread = bool(draft.is_thread and parts)
            if not is_thread:
                parts = [content]

            for idx, part in enumerate(parts, start=1):
                rows.append({
                    "run_id": run_id,
                    "persona": persona_key,
                    "text": part,
                    "thread_part_index": idx,
                    "thread_total": len(parts),
                    "is_thread": is_thread,
                    "angle": getattr(draft, "angle", ""),
                    "hook": getattr(draft, "hook", ""),
                    "cta": getattr(draft, "cta", ""),
                    "quality_score": draft.quality_score,
                    "source_topic": topic.get("topic", ""),
                    "source_url": topic.get("url", ""),
                    "content_type": topic.get("type", ""),
                    "status": status,
                    "created_at": created_at,
                })
        return rows

    def _append_master(self, rows: List[dict]):
        if not rows:
            return
        root = get_project_root()
        path = root / self.settings.master_csv_path
        path.parent.mkdir(parents=True, exist_ok=True)

        self._write_csv(path, rows, overwrite=False)

    def _write_csv(self, path: Path, rows: List[dict], overwrite: bool):
        if not rows:
            return
        fieldnames = list(rows[0].keys())
        if overwrite:
            mode = "w"
            write_header = True
        else:
            mode = "a"
            write_header = not path.exists()
        with open(path, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)


class GoogleSheetsExporter:
    """Append rows to a Google Sheet using a service account."""

    def __init__(self, sheet_id: str, sheet_name: str):
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self._service = None

    def _init_service(self):
        if self._service is not None:
            return

        try:
            from google.oauth2.service_account import Credentials  # type: ignore
            from googleapiclient.discovery import build  # type: ignore
        except Exception as e:
            raise RuntimeError("google-api-python-client and google-auth are required for Sheets export") from e

        creds_info = _load_service_account_info()
        if creds_info is None:
            raise RuntimeError("Missing Google service account credentials")

        creds = Credentials.from_service_account_info(
            creds_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    def append_rows(self, rows: List[dict]):
        if not rows:
            return
        self._init_service()

        header = list(rows[0].keys())
        values = [header] + [[row.get(h, "") for h in header] for row in rows]

        if not self._has_header(header):
            self._append_values(values)
        else:
            self._append_values(values[1:])

    def _has_header(self, header: List[str]) -> bool:
        result = self._service.spreadsheets().values().get(
            spreadsheetId=self.sheet_id,
            range=f"{self.sheet_name}!A1:Z1"
        ).execute()
        existing = result.get("values", [])
        if not existing:
            return False
        return existing[0][:len(header)] == header

    def _append_values(self, values: List[List[str]]):
        self._service.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"{self.sheet_name}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()


def _load_service_account_info() -> Optional[dict]:
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
    if path and Path(path).exists():
        return json.loads(Path(path).read_text())
    if b64:
        import base64
        return json.loads(base64.b64decode(b64).decode("utf-8"))
    return None


def export_rows(settings: ExportSettings, run_id: str, topic: dict, per_persona: Dict[str, object], status: str = "pending"):
    if not settings.enabled:
        return

    rows = CsvExporter(settings)._build_rows(run_id, topic, per_persona, status=status)
    if not rows:
        return

    if settings.format in ("csv", "both"):
        CsvExporter(settings).export_run(run_id, topic, per_persona, status=status)

    if settings.format in ("gsheets", "both"):
        sheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
        sheet_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Drafts")
        if not sheet_id:
            logger.warning("GOOGLE_SHEETS_SPREADSHEET_ID not set; skipping Sheets export")
            return
        try:
            GoogleSheetsExporter(sheet_id=sheet_id, sheet_name=sheet_name).append_rows(rows)
            logger.info(f"Run {run_id}: exported rows to Google Sheets")
        except Exception as e:
            logger.error(f"Run {run_id}: Sheets export failed: {e}")


def export_content_items(settings: ExportSettings, items: List[object]):
    if not settings.enabled or not items:
        return

    rows: List[dict] = []
    for item in items:
        rows.extend(_rows_from_content_item(item))

    if not rows:
        return

    if settings.format in ("csv", "both"):
        exporter = CsvExporter(settings)
        exporter._append_master(rows)
        run_id = getattr(items[0], "run_id", "") if items else ""
        if run_id:
            root = get_project_root()
            export_dir = root / settings.export_dir
            export_dir.mkdir(parents=True, exist_ok=True)
            path = export_dir / f"run_{run_id}.csv"
            exporter._write_csv(path, rows, overwrite=True)

    if settings.format in ("gsheets", "both"):
        sheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")
        sheet_name = os.getenv("GOOGLE_SHEETS_SHEET_NAME", "Drafts")
        if not sheet_id:
            logger.warning("GOOGLE_SHEETS_SPREADSHEET_ID not set; skipping Sheets export")
            return
        try:
            GoogleSheetsExporter(sheet_id=sheet_id, sheet_name=sheet_name).append_rows(rows)
            logger.info("Exported rows to Google Sheets")
        except Exception as e:
            logger.error(f"Sheets export failed: {e}")


def _rows_from_content_item(item) -> List[dict]:
    created_at = item.created_at.isoformat() if getattr(item, "created_at", None) else datetime.now(timezone.utc).isoformat()
    rows: List[dict] = []
    for persona in ("pro", "work", "degen"):
        content = getattr(item, f"{persona}_content", "") or ""
        thread_parts_raw = getattr(item, f"{persona}_thread_parts", "") or "[]"
        try:
            parts = json.loads(thread_parts_raw)
        except Exception:
            parts = []
        is_thread = bool(getattr(item, f"{persona}_is_thread", False) and parts)
        if not is_thread:
            parts = [content]

        for idx, part in enumerate(parts, start=1):
            rows.append({
                "run_id": getattr(item, "run_id", ""),
                "persona": persona,
                "text": part,
                "thread_part_index": idx,
                "thread_total": len(parts),
                "is_thread": is_thread,
                "angle": "",
                "hook": "",
                "cta": "",
                "quality_score": getattr(item, "quality_score", ""),
                "source_topic": getattr(item, "source_topic", ""),
                "source_url": getattr(item, "source_url", ""),
                "content_type": getattr(item, "content_type", ""),
                "status": getattr(item, "status", ""),
                "created_at": created_at,
            })
    return rows
