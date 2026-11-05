import logging
import time
from flask import render_template, request, jsonify, redirect, url_for, session, Response
from werkzeug.utils import secure_filename
from app.helpers.authentication import supabase_admin
from app.helpers.paging import fetch_all_rows
from app.helpers.auth_decorators import api_instructor_required, login_required
from app.helpers.class_access import _instructor_owns_class, _owned_class_row
from app.helpers.class_removal import delete_class_rows
from app.helpers.class_archive import (
    _ARCHIVE_ID,
    _RESTORE_FAILED,
    _RESTORE_TABLES,
    _archive_size_label,
    _delete_restore_attempt,
    _owned_archive_row,
    _remove_archive_object,
    _restore_chunks,
    _restore_class_id,
    _restore_error_text,
    _restore_known_message,
    _zip_over_archive_limit,
)
from app.helpers.api_input import _safe_api_error
from app.helpers.rate_limit import rate_limited
from uuid import uuid4
from app.routes.blueprint import main_bp

logger = logging.getLogger(__name__)

@main_bp.route("/class/<class_id>/archive", methods=["POST"])
@api_instructor_required
@rate_limited("archive_create", limit=5, window_sec=3600)
def archive_class(class_id):
    from app.helpers.archive_export import build_class_archive_zip, check_archive_manifest
    from app.helpers.archive_snapshot import build_class_snapshot
    from app.helpers.archive_storage import (
        download_archive,
        remove_archive,
        upload_archive,
        upload_snapshot,
    )

    kept_error = (
        "The class was archived, but it was not fully removed. "
        "The archive is saved under Archived Classes. "
        "Delete the class to finish removing it."
    )
    budget_error = (
        "The archive took too long, or the class changed. Nothing was removed. Try again."
    )
    instructor_id = session["user_id"]
    class_row = _owned_class_row(class_id, instructor_id)
    if class_row is None:
        return jsonify({
            "success": False,
            "error": "This class was not found. It may already be archived.",
        }), 404
    if request.form.get("confirm_name") != class_row.get("name"):
        return jsonify({
            "success": False,
            "error": "Type the class name exactly to archive it.",
        }), 400

    archive_id = str(uuid4())
    path = f"{instructor_id}/{archive_id}.zip"
    snapshot_path = f"{instructor_id}/{archive_id}.snapshot.json.gz"
    start = time.monotonic()

    def discard(message):
        for object_path in (snapshot_path, path):
            try:
                remove_archive(object_path)
            except Exception:
                logger.error("Could not remove archive at %s", object_path)
        return jsonify({"success": False, "error": message}), 500

    try:
        zip_bytes, manifest = build_class_archive_zip(class_id)
        check_archive_manifest(manifest, zip_bytes)
    except Exception as exc:
        logger.error("Could not build archive for class %s: %s", class_id, exc)
        return jsonify({
            "success": False,
            "error": "Could not build the archive. Nothing was changed.",
        }), 500
    too_big = _zip_over_archive_limit(zip_bytes)
    if too_big is not None:
        return too_big
    try:
        snapshot_bytes = build_class_snapshot(class_id, manifest)
    except Exception as exc:
        logger.error("Could not build snapshot for class %s: %s", class_id, exc)
        return jsonify({
            "success": False,
            "error": "Could not build the archive. Nothing was changed.",
        }), 500
    if time.monotonic() - start > 200:
        return jsonify({"success": False, "error": budget_error}), 500
    try:
        upload_archive(path, zip_bytes)
    except Exception as exc:
        logger.error("Could not upload archive %s: %s", path, exc)
        return discard("Could not save the archive. The class was not changed.")
    try:
        upload_snapshot(snapshot_path, snapshot_bytes)
    except Exception as exc:
        logger.error("Could not upload snapshot %s: %s", snapshot_path, exc)
        return discard("Could not save the archive. The class was not changed.")
    try:
        saved = download_archive(path)
        mismatch = saved != zip_bytes
        check_archive_manifest(manifest, saved)
        if mismatch:
            raise ValueError("downloaded archive bytes do not match the upload")
        del saved
        saved_snapshot = download_archive(snapshot_path)
        if saved_snapshot != snapshot_bytes:
            raise ValueError("downloaded snapshot bytes do not match the upload")
        del saved_snapshot
        size_bytes = len(zip_bytes)
        del zip_bytes
        del snapshot_bytes
    except Exception as exc:
        logger.error("Archive %s failed verification: %s", path, exc)
        return discard(
            "The saved archive did not verify and was removed. The class was not changed."
        )
    try:
        again = _owned_class_row(class_id, instructor_id)
    except Exception as exc:
        logger.error("Could not re-check class %s before recording the archive: %s", class_id, exc)
        return discard(budget_error)
    if again is None or time.monotonic() - start > 200:
        return discard(budget_error)
    record = {
        "id": archive_id,
        "instructor_id": instructor_id,
        "class_name": again.get("name"),
        "semester": again.get("semester"),
        "days": again.get("days"),
        "section_number": again.get("section_number"),
        "student_count": manifest.get("source_students"),
        "assignment_count": manifest.get("source_assignments"),
        "storage_path": path,
        "snapshot_path": snapshot_path,
        "size_bytes": size_bytes,
    }
    try:
        supabase_admin.table("archived_classes").insert(record).execute()
        logger.info(
            "archive create user_id=%s class_id=%s",
            instructor_id,
            class_id,
        )
    except Exception as exc:
        logger.error("Could not record archive %s: %s", archive_id, exc)
        try:
            supabase_admin.table("archived_classes").delete().eq("id", archive_id).execute()
        except Exception as delete_exc:
            logger.error("Could not delete archive row %s: %s", archive_id, delete_exc)
        return discard("Could not record the archive. The class was not changed.")
    try:
        still_owned = _owned_class_row(class_id, instructor_id)
    except Exception as exc:
        logger.error(
            "Could not re-check class %s after recording archive %s: %s",
            class_id, archive_id, exc,
        )
        return jsonify({"success": False, "archived": True, "error": kept_error}), 500
    if still_owned is None:
        return jsonify({
            "success": True,
            "redirect": url_for("main.instructor_dashboard"),
        })
    try:
        delete_class_rows(class_id, instructor_id)
    except Exception as exc:
        logger.error(
            "Archive %s saved but class %s was not fully removed: %s",
            archive_id, class_id, exc,
        )
        return jsonify({"success": False, "archived": True, "error": kept_error}), 500
    return jsonify({
        "success": True,
        "redirect": url_for("main.instructor_dashboard"),
    })

@main_bp.route("/archived-classes")
@login_required
def archived_classes():
    if session.get("role") != "instructor":
        return redirect(url_for("main.login_page"))
    instructor_id = session["user_id"]
    rows = fetch_all_rows(
        supabase_admin.table("archived_classes")
        .select(
            "id, class_name, semester, days, section_number, "
            "student_count, assignment_count, size_bytes, archived_at, "
            "snapshot_path, restored_at, restored_class_id"
        )
        .eq("instructor_id", instructor_id)
        .order("archived_at", desc=True)
        .order("id", desc=True)
    )
    archives = []
    for row in rows:
        parts = [row.get("semester"), row.get("days"), row.get("section_number")]
        snapshot_path = str(row.get("snapshot_path") or "").strip()
        restored_at = row.get("restored_at") or ""
        restored_class_id = str(row.get("restored_class_id") or "").strip()
        archives.append({
            "id": row.get("id"),
            "class_name": row.get("class_name") or "",
            "detail": " · ".join(str(part) for part in parts if part),
            "student_count": row.get("student_count") or 0,
            "assignment_count": row.get("assignment_count") or 0,
            "archived_at": row.get("archived_at") or "",
            "size_label": _archive_size_label(row.get("size_bytes")),
            "restored": bool(restored_at),
            "restorable": bool(snapshot_path) and not restored_at,
            "restored_at": restored_at,
            "restored_class_id": restored_class_id if restored_at else "",
        })
    return render_template("classes/archived_classes.html", archives=archives)

@main_bp.route("/archived-classes/<archive_id>/download")
@login_required
@rate_limited("archive_download", limit=30, window_sec=3600)
def download_archived_class(archive_id):
    if session.get("role") != "instructor":
        return redirect(url_for("main.login_page"))
    from app.helpers.archive_storage import download_archive
    row = _owned_archive_row(archive_id, session["user_id"])
    if row is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    try:
        payload = download_archive(row.get("storage_path") or "")
    except Exception as exc:
        logger.error("Could not download archive %s: %s", archive_id, exc)
        return _safe_api_error("Could not download the archive", 500, log_detail=exc)
    filename = f"{secure_filename(str(row.get('class_name') or '')) or 'class'}_archive.zip"
    logger.info(
        "archive download user_id=%s archive_id=%s",
        session.get("user_id"),
        archive_id,
    )
    return Response(
        payload,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@main_bp.route("/archived-classes/<archive_id>/delete", methods=["POST"])
@api_instructor_required
def delete_archived_class(archive_id):
    if _ARCHIVE_ID.fullmatch(str(archive_id or "")) is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    instructor_id = session["user_id"]
    row = _owned_archive_row(archive_id, instructor_id)
    if row is None:
        return jsonify({
            "success": True,
            "redirect": url_for("main.archived_classes"),
        })
    if request.form.get("confirm_name") != row.get("class_name"):
        return jsonify({
            "success": False,
            "error": "Type the class name exactly to delete this archive.",
        }), 400
    try:
        _remove_archive_object(row.get("storage_path") or "")
        _remove_archive_object(row.get("snapshot_path") or "")
    except Exception as exc:
        logger.error("Could not remove archive %s: %s", archive_id, exc)
        return jsonify({
            "success": False,
            "error": "Could not delete the archive file. The record was kept.",
        }), 500
    try:
        supabase_admin.table("archived_classes").delete().eq("id", archive_id).eq(
            "instructor_id", instructor_id
        ).execute()
    except Exception as exc:
        logger.error(
            "Could not delete archive row %s after the file was removed: %s",
            archive_id, exc,
        )
        return jsonify({
            "success": False,
            "error": "The file was deleted but the record could not be removed. Try again.",
        }), 500
    return jsonify({
        "success": True,
        "redirect": url_for("main.archived_classes"),
    })

@main_bp.route("/archived-classes/<archive_id>/restore", methods=["POST"])
@api_instructor_required
@rate_limited("archive_restore", limit=5, window_sec=3600)
def restore_archived_class(archive_id):
    import json
    from app.helpers.archive_snapshot import GzipTooLarge, gunzip_bounded
    from app.helpers.archive_storage import download_archive

    start = time.monotonic()
    if _ARCHIVE_ID.fullmatch(str(archive_id or "")) is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    instructor_id = session["user_id"]
    row = _owned_archive_row(archive_id, instructor_id)
    if row is None:
        return jsonify({"success": False, "error": "This archive was not found."}), 404
    if not str(row.get("snapshot_path") or "").strip():
        return jsonify({
            "success": False,
            "error": "This archive is not restorable.",
        }), 409
    if row.get("restored_at"):
        return jsonify({
            "success": False,
            "error": "This archive has already been restored.",
        }), 409

    try:
        blob = download_archive(row.get("snapshot_path") or "")
        try:
            raw = gunzip_bounded(blob)
        finally:
            del blob
    except GzipTooLarge:
        return jsonify({
            "success": False,
            "error": "This archive is too large to restore. Nothing was changed.",
        }), 413
    except Exception as exc:
        logger.error("Could not read snapshot for archive %s: %s", archive_id, exc)
        return jsonify({"success": False, "error": _RESTORE_FAILED}), 500
    try:
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.error("Could not read snapshot for archive %s: %s", archive_id, exc)
        return jsonify({"success": False, "error": _RESTORE_FAILED}), 500
    finally:
        del raw
    if not isinstance(document, dict) or document.get("format_version") != 1:
        return jsonify({
            "success": False,
            "error": "This archive is not restorable.",
        }), 409

    loaded = {}
    for table_name in _RESTORE_TABLES:
        rows = document.pop(table_name, [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            return jsonify({
                "success": False,
                "error": "This archive is not restorable.",
            }), 409
        loaded[table_name] = rows
    del document

    attempt_id = str(uuid4())
    staged = False
    counts = {}
    try:
        for table_name in _RESTORE_TABLES:
            rows = loaded.pop(table_name)
            records = [
                {
                    "attempt_id": attempt_id,
                    "table_name": table_name,
                    "seq": index,
                    "row_data": item,
                }
                for index, item in enumerate(rows, start=1)
            ]
            counts[table_name] = len(records)
            del rows
            for chunk in _restore_chunks(records):
                if time.monotonic() - start > 240:
                    _delete_restore_attempt(attempt_id)
                    return jsonify({
                        "success": False,
                        "error": "The restore took too long. Nothing was changed.",
                    }), 500
                staged = True
                supabase_admin.table("archive_restore_stage").insert(chunk).execute()
            del records
        if time.monotonic() - start > 240:
            _delete_restore_attempt(attempt_id)
            return jsonify({
                "success": False,
                "error": "The restore took too long. Nothing was changed.",
            }), 500
        result = supabase_admin.rpc(
            "restore_archived_class",
            {
                "p_archive_id": archive_id,
                "p_instructor_id": instructor_id,
                "p_attempt_id": attempt_id,
                "p_expected": counts,
            },
        ).execute()
        class_id = _restore_class_id(result.data)
        if class_id is None:
            logger.error(
                "Restore of archive %s returned no class id: %s",
                archive_id,
                result.data,
            )
            return jsonify({
                "success": True,
                "redirect": url_for("main.instructor_dashboard"),
            })
    except Exception as exc:
        if staged:
            _delete_restore_attempt(attempt_id)
        known = _restore_known_message(_restore_error_text(exc))
        if known:
            return jsonify({"success": False, "error": known}), 409
        logger.error("Could not restore archive %s: %s", archive_id, exc)
        return jsonify({"success": False, "error": _RESTORE_FAILED}), 500
    try:
        _remove_archive_object(row.get("snapshot_path") or "")
        _remove_archive_object(row.get("storage_path") or "")
        supabase_admin.table("archived_classes").delete().eq(
            "id", archive_id
        ).eq("instructor_id", instructor_id).execute()
    except Exception as exc:
        logger.error("Could not remove restored archive %s: %s", archive_id, exc)
    logger.info(
        "archive restore user_id=%s class_id=%s",
        instructor_id,
        class_id,
    )
    return jsonify({
        "success": True,
        "redirect": url_for("main.class_detail", class_id=class_id),
    })

@main_bp.route("/class/<class_id>/export-archive")
@login_required
@rate_limited("archive_export", limit=5, window_sec=3600)
def export_class_archive(class_id):
    """Download a class archive zip. Does not delete the class or write a row."""
    if not _instructor_owns_class(class_id):
        return redirect(url_for("main.instructor_dashboard"))
    from app.helpers.archive_export import (
        ArchiveClassNotFound,
        ArchiveManifestError,
        build_class_archive_zip,
    )
    try:
        zip_bytes, manifest = build_class_archive_zip(class_id)
    except ArchiveClassNotFound:
        return redirect(url_for("main.instructor_dashboard"))
    except ArchiveManifestError as exc:
        return _safe_api_error("Could not export archive", 500, log_detail=exc)
    too_big = _zip_over_archive_limit(zip_bytes)
    if too_big is not None:
        return too_big
    filename = f"{secure_filename(str(manifest.get('class_name') or '')) or 'class'}_archive.zip"
    return Response(
        zip_bytes,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

