async def handle_upload(request):
    data = open(request.file_path, "rb").read()
    await process(data)
    return {"status": "ok"}
