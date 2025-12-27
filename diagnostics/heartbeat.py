import time

def heartbeat_monitor(interval=10, max_missed=30):
    missed = 0
    while True:
        print(f"[💓] Reflexion heartbeat at {time.strftime('%H:%M:%S')}")
        time.sleep(interval)
        missed += 1
        if missed > max_missed:
            print("[⚠️] Heartbeat missed too many cycles. Check ingestion.")
            missed = 0