from app.authentication import supabase_admin

_BUCKET = "class-archives"


def upload_archive(path, data):
    supabase_admin.storage.from_(_BUCKET).upload(
        path,
        data,
        {"content-type": "application/zip", "upsert": "false"},
    )


def download_archive(path) -> bytes:
    return supabase_admin.storage.from_(_BUCKET).download(path)


def remove_archive(path):
    supabase_admin.storage.from_(_BUCKET).remove([path])
