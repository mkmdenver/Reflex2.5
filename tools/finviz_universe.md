Build the midcap universe list (manifest only)

python tools\finviz_universe.py universe ^
  --universe-tag universe_midcaps_v1 ^
  --out-csv watchlists\universe_midcaps_v1_2025-12-20.csv ^
  --out-txt watchlists\universe_midcaps_v1_2025-12-20.txt

Build + write to Postgres + scrape fundamentals

python finviz_universe.py universe ^
  --universe-tag universe_midcaps_v1 ^
  --out-csv watchlists\universe_midcaps_v1_2025-12-20.csv ^
  --pg --enrich --sleep-s 1.2

 Add one symbol (your “high volume to track”) 
python tools\finviz_universe.py add-one NVDA --universe-tag high_volume_watch --pg --enrich


---------------------

------ use this

---------------------

python finviz_prime_pump.py --sleep-s 1.2 universe ^
  --tag universe_midcaps_v1 ^
  --out-csv watchlists\universe_midcaps_v1_2025-12-20.csv ^
  --capture-fundamentals ^
  --pg
