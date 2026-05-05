from localnetftp.share.download_server import lan_download_urls


def test_lan_download_urls_builds_links_for_addresses():
    urls = lan_download_urls(49300, "LocalNetFTP.exe", ["192.168.1.10", "10.0.0.5"])

    assert urls == [
        "http://192.168.1.10:49300/download/LocalNetFTP.exe",
        "http://10.0.0.5:49300/download/LocalNetFTP.exe",
    ]
