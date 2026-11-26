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
