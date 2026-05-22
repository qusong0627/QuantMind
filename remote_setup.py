import subprocess
import time

commands = [
    # 1. Grants
    "sudo -u postgres psql -d quantmind -c \"CREATE USER quantmind_market WITH ENCRYPTED PASSWORD 'quantmind_market_2026';\"",
    "sudo -u postgres psql -d quantmind -c \"GRANT CONNECT ON DATABASE quantmind TO quantmind_market; GRANT USAGE ON SCHEMA public TO quantmind_market; GRANT SELECT ON ALL TABLES IN SCHEMA public TO quantmind_market; ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO quantmind_market;\"",
    # 2. listen_addresses
    "sudo sed -i \"s/^#*listen_addresses.*=.*/listen_addresses = '*'/g\" $(sudo find /etc/postgresql -name postgresql.conf)",
    # 3. pg_hba.conf
    "for f in $(sudo find /etc/postgresql -name pg_hba.conf); do grep -q 'quantmind_market' $f || echo 'host quantmind quantmind_market 0.0.0.0/0 scram-sha-256' | sudo tee -a $f; done",
    # 4. Restart
    "sudo systemctl restart postgresql"
]

for cmd in commands:
    print(f"Running: {cmd}")
    res = subprocess.run(["ssh", "-o", "BatchMode=yes", "quantmind-redis", cmd], capture_output=True, text=True)
    if res.returncode != 0:
        if "already exists" not in res.stderr and "CREATE ROLE" not in res.stdout:
            print(f"Error: {res.stderr}")
    else:
        print(f"Output: {res.stdout}")
