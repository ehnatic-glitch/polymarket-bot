import requests

def test_internet():
    r = requests.get("https://example.com", timeout=10)
    return r.status_code

if __name__ == "__main__":
    print("Status:", test_internet())