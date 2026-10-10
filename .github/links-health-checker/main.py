"""
Simple script to check URLs availability.

This script parses README file, collects all URLs, checks their availability and writes down unavailable ones.
Unavailability means status code of the response was not 200.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from urllib.parse import urlparse

import mistletoe
import requests
from bs4 import BeautifulSoup

from app.runtime_config import config_integer

if __name__ == "__main__":
    print("Checking README's outer links are available")

    # parse markdown
    with open('README.md', 'r') as file:
        rendered = mistletoe.markdown(file)

    parsed_html = BeautifulSoup(rendered, features='html.parser')
    anchors = parsed_html.find_all('a', attrs={'href': True})

    # for every link check the availability
    broken_links = []
    for a in anchors:
        href = a['href']
        url = urlparse(href)
        if url.scheme:
            response = requests.get(href, allow_redirects=True, timeout=config_integer("link_checker", "timeout_seconds"))
            if response.status_code == 200:
                print(f"URL `{href}` is accessible")
            else:
                print(f"URL `{href}` is not accessible")
                broken_links.append(href)

    # write down all broken links in the file
    file_name = 'broken-links.txt'
    if broken_links:
        print(f"Found {len(broken_links)} broken link(s): {broken_links}")
        with open(file_name, 'w') as file:
            for bl in broken_links:
                file.write(f"{bl}\n")
    else:
        print("No broken links were found")
