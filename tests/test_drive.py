from drivepull.drive import parse_link, safe_name, _ENTRY

def test_parse():
    f = "1AbCdEfGhIjKlMnOpQrStUv"
    assert parse_link(f"https://drive.google.com/drive/folders/{f}?usp=sharing").kind == "folder"
    assert parse_link(f"https://drive.google.com/drive/u/1/folders/{f}").id == f
    l = parse_link(f"https://drive.google.com/file/d/{f}/view?usp=drive_link&resourcekey=0-abc")
    assert (l.kind, l.id, l.resource_key) == ("file", f, "0-abc")
    assert parse_link(f"https://drive.google.com/open?id={f}").id == f
    assert parse_link(f"https://docs.google.com/document/d/{f}/edit").kind == "file"
    assert parse_link(f).kind == "unknown"
    assert parse_link("https://example.com/x") is None

def test_safe():
    assert safe_name('a:b/c?.mov') == "a_b_c_.mov"
    assert safe_name("CON.txt") == "_CON.txt"

def test_entry_regex():
    h = ('<div class="flip-entry" id="entry-1abcdefghijk" tabindex="0"><div class="flip-entry-info">'
         '<a href="https://drive.google.com/drive/folders/1abcdefghijk" target="_blank"><div class="flip-entry-title">Day&#39;1</div>')
    assert _ENTRY.findall(h) == [("1abcdefghijk", "https://drive.google.com/drive/folders/1abcdefghijk", "Day&#39;1")]


def test_mac_hangul_nfd_to_nfc():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "한글_촬영본_최종.mov")
    assert nfd != "한글_촬영본_최종.mov"            # 맥에서 올라온 이름 (자모 분리)
    assert safe_name(nfd) == "한글_촬영본_최종.mov"  # 받을 땐 조합형


def test_blocked_page_detected():
    """'소유자가 다운로드를 막음' 안내 페이지는 재시도하지 않고 바로 DownloadBlocked."""
    import pytest
    from drivepull.drive import DownloadBlocked, PublicSource

    class R:
        status_code = 200
        url = "https://drive.usercontent.google.com/download?id=x"
        text = ("<html><head><title>Google Drive - Can&#39;t download file</title></head><body>"
                "Sorry, the owner hasn&#39;t given you permission to download this file. "
                "Only the owner and editors can download this file.</body></html>").replace("&#39;", "'")
    with pytest.raises(DownloadBlocked):
        PublicSource("x")._check_html(R())
