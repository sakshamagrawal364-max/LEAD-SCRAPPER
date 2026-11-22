# Lead scraper (OpenStreetMap + ScrapeGraphAI + Ollama)

Finds businesses in real estate, healthcare, consulting and coaching across
US, UK, Canadian and other cities, then reads each business's own website with a
local Ollama model to pull a contact email, phone, services and a description.
Output: `leads.csv`.

## Setup (on your own computer)

1. Install Ollama from https://ollama.com and pull a model:
   ```bash
   ollama pull llama3.2:3b
   ```
2. Install the Python dependencies and the browser that ScrapeGraphAI uses:
   ```bash
   pip install scrapegraphai requests pydantic
   playwright install chromium
   ```

## Run

```bash
# Quick check: 3 leads, one city, one category
python leads.py --cities "Toronto,CA" --categories real_estate --max-per-city 3

# Fast OSM-only list (no website visits, minutes instead of hours)
python leads.py --no-enrich --max-per-city 50

# Full run: all categories x 28 default cities, 15 leads each (~1,600 max)
python leads.py
```

Useful options:

| Option | Meaning |
| --- | --- |
| `--cities "Austin,US" "Cork,IE"` | Cities to search, as `City,CountryCode` |
| `--categories real_estate healthcare consultants coaching` | Which categories |
| `--max-per-city 15` | Leads per city per category |
| `--model llama3.1:8b` | Any Ollama model you have pulled |
| `--delay 3` | Seconds between websites |
| `--output leads.csv` | Output file |

The CSV is written after every lead, so you can stop with Ctrl+C and run the
same command again: leads already in the file are skipped.

## CSV columns

`category, name, website, email, phone, address, city, country, services,
description, osm_url, enriched`

`enriched` is `yes`, `failed` (site could not be read), `robots_disallowed`, or
`no` (run with `--no-enrich`).

## Notes

- Only businesses that list a website on OpenStreetMap are included.
- Coaches are underrepresented: many work online without a mapped office.
- Speed: on CPU a small model takes 1-3 minutes per website. With a GPU it is
  usually a few seconds. Use `--no-enrich` first if you just need the list.
- The script respects `robots.txt`, waits between requests, and follows the
  Nominatim limit of 1 request per second.
- OpenStreetMap data is © OpenStreetMap contributors (ODbL). Check privacy and
  email laws (GDPR/UK GDPR, CASL in Canada, CAN-SPAM in the US) before
  contacting leads.
