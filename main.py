import os
import subprocess
import sys
from pathlib import Path

# ── Project / App metadata ──────────────────────────────────────────────────
PROJECT_NAME   = "faizun1"
APP_NAME       = "python-flask"
REGION         = "us-east-1"
TARGET         = "ec2"
PYTHON_VERSION = "3.11"
ENTRYPOINT     = "app.main:app"
APP_PORT       = 3000          # exposed / nginx front-end port
GUNICORN_PORT  = 8000          # internal gunicorn bind port
DB_TYPE        = "none"

# ── Directory layout ────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
ANSIBLE_DIR   = BASE_DIR / "ansible"
INVENTORY_DIR = ANSIBLE_DIR / "inventory"
ROLES_DIR     = ANSIBLE_DIR / "roles" / "deploy"

def create_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  [created] {path}")

# ═══════════════════════════════════════════════════════════════════════════
# FILE CONTENT GENERATORS
# ═══════════════════════════════════════════════════════════════════════════

def ansible_cfg() -> str:
    return """\
[defaults]
host_key_checking = False
roles_path        = roles
retry_files_enabled = False
"""

def inventory_template() -> str:
    return """\
[app]
{{ ec2_host }} ansible_user=ubuntu ansible_ssh_private_key_file={{ ssh_key_path }}
"""

def playbook_yml() -> str:
    return f"""\
---
- name: Deploy {APP_NAME} on EC2 (direct)
  hosts: app
  become: yes

  vars:
    app_dir:        /opt/app
    venv_dir:       /opt/app/venv
    python_bin:     python{PYTHON_VERSION}
    entrypoint:     "{ENTRYPOINT}"
    gunicorn_port:  {GUNICORN_PORT}
    app_user:       ubuntu

    # Database vars (passed via -e; harmless when empty)
    database_url:      "{{{{ database_url    | default('') }}}}"
    mongo_uri:         "{{{{ mongo_uri       | default('') }}}}"
    db_host:           "{{{{ db_host         | default('') }}}}"
    db_port:           "{{{{ db_port         | default('') }}}}"
    db_name:           "{{{{ db_name         | default('') }}}}"
    db_user:           "{{{{ db_user         | default('') }}}}"
    db_username:       "{{{{ db_username     | default('') }}}}"
    db_password:       "{{{{ db_password     | default('') }}}}"
    db_password_plain: "{{{{ db_password_plain | default('') }}}}"
    db_type:           "{{{{ db_type         | default('none') }}}}"
    install_db:        "{{{{ install_db      | default('false') }}}}"

  tasks:

    # ── 0. System packages ────────────────────────────────────────────────
    - name: Update apt cache
      apt:
        update_cache: yes
        cache_valid_time: 3600

    - name: Install system dependencies
      apt:
        name:
          - python{PYTHON_VERSION}
          - python{PYTHON_VERSION}-venv
          - python3-pip
          - nginx
          - git
          - curl
        state: present

    # ── 1. Optional local DB install ──────────────────────────────────────
    - name: Install PostgreSQL (local)
      when: install_db == 'true' and db_type == 'postgres'
      block:
        - name: apt install postgresql
          apt:
            name: postgresql
            state: present

        - name: Fix pg_hba SCRAM → md5
          shell: |
            HBA=$(find /etc/postgresql -name pg_hba.conf | head -1)
            sed -i 's/scram-sha-256/md5/g' "$HBA"
            systemctl reload postgresql
          args:
            executable: /bin/bash

        - name: Create PostgreSQL database
          shell: |
            sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='{{{{ db_name }}}}'" | grep -q 1 || \
            sudo -u postgres psql -c "CREATE DATABASE {{{{ db_name }}}};"
          args:
            executable: /bin/bash

        - name: Create PostgreSQL user
          shell: |
            sudo -u postgres psql -c "CREATE USER {{{{ db_username }}}} WITH ENCRYPTED PASSWORD '{{{{ db_password_plain }}}}';" || true
          args:
            executable: /bin/bash

        - name: Grant privileges
          shell: |
            sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE {{{{ db_name }}}} TO {{{{ db_username }}}};"
          args:
            executable: /bin/bash

    - name: Install MySQL (local)
      when: install_db == 'true' and db_type == 'mysql'
      block:
        - name: apt install mysql-server
          apt:
            name: mysql-server
            state: present

        - name: Create MySQL database and user
          shell: |
            mysql -e "CREATE DATABASE IF NOT EXISTS {{{{ db_name }}}};"
            mysql -e "CREATE USER IF NOT EXISTS '{{{{ db_username }}}}'@'localhost' IDENTIFIED BY '{{{{ db_password_plain }}}}';"
            mysql -e "GRANT ALL ON {{{{ db_name }}}}.* TO '{{{{ db_username }}}}'@'localhost';"
          args:
            executable: /bin/bash

    - name: Install MongoDB (local)
      when: install_db == 'true' and db_type == 'mongodb'
      block:
        - name: apt install mongodb-org
          apt:
            name: mongodb-org
            state: present

        - name: Start and enable mongod
          systemd:
            name: mongod
            state: started
            enabled: yes

        - name: Create MongoDB collection
          shell: |
            mongosh --eval 'db.getSiblingDB("{{{{ db_name }}}}").createCollection("init")'
          args:
            executable: /bin/bash

    # ── 2. Application directory ──────────────────────────────────────────
    - name: Create app directory
      file:
        path: "{{{{ app_dir }}}}"
        state: directory
        owner: "{{{{ app_user }}}}"
        group: "{{{{ app_user }}}}"
        mode: "0755"

    # ── 3. Clone / pull repository ────────────────────────────────────────
    - name: Check if git repo already exists
      stat:
        path: "{{{{ app_dir }}}}/.git"
      register: git_repo

    - name: Clone repository
      when: not git_repo.stat.exists
      become_user: "{{{{ app_user }}}}"
      git:
        repo:    "{{{{ repo_url }}}}"
        dest:    "{{{{ app_dir }}}}"
        version: "{{{{ git_branch | default('main') }}}}"
        force:   yes

    - name: Pull latest code
      when: git_repo.stat.exists
      become_user: "{{{{ app_user }}}}"
      git:
        repo:    "{{{{ repo_url }}}}"
        dest:    "{{{{ app_dir }}}}"
        version: "{{{{ git_branch | default('main') }}}}"
        force:   yes
        update:  yes

    # ── 4. Write .env (shell approach — safe when no DB configured) ───────
    - name: Write /opt/app/.env
      shell: |
        printf '' > {{{{ app_dir }}}}/.env
        [ -n "{{{{ database_url }}}}" ]      && echo "DATABASE_URL={{{{ database_url }}}}"           >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ mongo_uri }}}}" ]         && echo "MONGO_URI={{{{ mongo_uri }}}}"                 >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_host }}}}" ]           && echo "DB_HOST={{{{ db_host }}}}"                     >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_port }}}}" ]           && echo "DB_PORT={{{{ db_port }}}}"                     >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_name }}}}" ]           && echo "DB_NAME={{{{ db_name }}}}"                     >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_user }}}}" ]           && echo "DB_USER={{{{ db_user }}}}"                     >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_username }}}}" ]       && echo "DB_USERNAME={{{{ db_username }}}}"             >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_password }}}}" ]       && echo "DB_PASSWORD={{{{ db_password }}}}"             >> {{{{ app_dir }}}}/.env || true
        [ -n "{{{{ db_password_plain }}}}" ] && echo "DB_PASSWORD_PLAIN={{{{ db_password_plain }}}}" >> {{{{ app_dir }}}}/.env || true
      args:
        executable: /bin/bash

    # ── 5. Python venv + dependencies ─────────────────────────────────────
    - name: Create Python virtual environment
      become_user: "{{{{ app_user }}}}"
      command: "{{{{ python_bin }}}} -m venv {{{{ venv_dir }}}}"
      args:
        creates: "{{{{ venv_dir }}}}/bin/activate"

    - name: Upgrade pip
      become_user: "{{{{ app_user }}}}"
      pip:
        name:       pip
        state:      latest
        virtualenv: "{{{{ venv_dir }}}}"

    - name: Install Python requirements
      become_user: "{{{{ app_user }}}}"
      pip:
        requirements: "{{{{ app_dir }}}}/requirements.txt"
        virtualenv:   "{{{{ venv_dir }}}}"

    - name: Install gunicorn and uvicorn in venv
      become_user: "{{{{ app_user }}}}"
      pip:
        name:
          - gunicorn
          - uvicorn[standard]
        virtualenv: "{{{{ venv_dir }}}}"

    # ── 6. systemd service ────────────────────────────────────────────────
    - name: Write systemd service unit
      copy:
        dest: /etc/systemd/system/{APP_NAME}.service
        content: |
          [Unit]
          Description={APP_NAME} Python application
          After=network.target

          [Service]
          User={{{{ app_user }}}}
          WorkingDirectory={{{{ app_dir }}}}
          EnvironmentFile={{{{ app_dir }}}}/.env
          ExecStart={{{{ venv_dir }}}}/bin/gunicorn {{{{ entrypoint }}}} \
                    -w 4 -k uvicorn.workers.UvicornWorker \
                    -b 0.0.0.0:{{{{ gunicorn_port }}}}
          Restart=always
          RestartSec=5

          [Install]
          WantedBy=multi-user.target

    - name: Reload systemd daemon
      systemd:
        daemon_reload: yes

    - name: Enable and restart app service
      systemd:
        name:    "{APP_NAME}"
        state:   restarted
        enabled: yes

    # ── 7. Nginx reverse proxy ────────────────────────────────────────────
    - name: Write nginx site config
      copy:
        dest: /etc/nginx/sites-available/{APP_NAME}
        content: |
          server {{{{
              listen 80;
              server_name _;

              location / {{{{
                  proxy_pass         http://127.0.0.1:{{{{ gunicorn_port }}}};
                  proxy_http_version 1.1;
                  proxy_set_header   Upgrade $http_upgrade;
                  proxy_set_header   Connection "upgrade";
                  proxy_set_header   Host $host;
                  proxy_set_header   X-Real-IP $remote_addr;
                  proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
                  proxy_read_timeout 300;
              }}}}
          }}}}

    - name: Enable nginx site
      file:
        src:   /etc/nginx/sites-available/{APP_NAME}
        dest:  /etc/nginx/sites-enabled/{APP_NAME}
        state: link

    - name: Remove nginx default site
      file:
        path:  /etc/nginx/sites-enabled/default
        state: absent

    - name: Restart nginx
      systemd:
        name:  nginx
        state: restarted

    # ── 8. Verification ───────────────────────────────────────────────────
    - name: Wait for app to be reachable on port {GUNICORN_PORT}
      wait_for:
        host:    127.0.0.1
        port:    {GUNICORN_PORT}
        timeout: 120
        delay:   10
      register: wait_result
      ignore_errors: yes

    - name: Dump journalctl on failure
      when: wait_result is failed
      command: journalctl -u {APP_NAME} --no-pager -n 60
      register: journal_out

    - name: Show journal output
      when: wait_result is failed
      debug:
        msg: "{{{{ journal_out.stdout_lines }}}}"

    - name: Fail if app did not start
      when: wait_result is failed
      fail:
        msg: "{APP_NAME} failed to start — see journal output above."

    - name: Report server IP
      debug:
        msg: "server_ip={{{{ ansible_host }}}}"
"""

def deploy_yml() -> str:
    return f"""\
name: Deploy {APP_NAME} to EC2

on:
  push:
    branches: [ main, master ]
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python {PYTHON_VERSION}
        uses: actions/setup-python@v5
        with:
          python-version: "{PYTHON_VERSION}"

      - name: Install Ansible
        run: |
          pip install --upgrade pip
          pip install ansible

      - name: Write SSH private key
        run: |
          mkdir -p ~/.ssh
          echo "${{{{ secrets.EC2_SSH_KEY }}}}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          ssh-keyscan -H ${{{{ secrets.EC2_HOST }}}} >> ~/.ssh/known_hosts 2>/dev/null || true

      - name: Write Ansible inventory
        run: |
          mkdir -p ansible/inventory
          cat > ansible/inventory/hosts.ini <<EOF
          [app]
          ${{{{ secrets.EC2_HOST }}}} ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/deploy_key
          EOF

      - name: Run Ansible playbook
        run: |
          ansible-playbook ansible/playbook.yml \
            -i ansible/inventory/hosts.ini \
            -e repo_url='${{{{ secrets.REPO_URL }}}}' \
            -e git_branch='${{{{ secrets.GIT_BRANCH || 'main' }}}}' \
            -e database_url='${{{{ secrets.DATABASE_URL }}}}' \
            -e mongo_uri='${{{{ secrets.MONGO_URI }}}}' \
            -e db_host='${{{{ secrets.DB_HOST }}}}' \
            -e db_port='${{{{ secrets.DB_PORT }}}}' \
            -e db_name='${{{{ secrets.DB_NAME }}}}' \
            -e db_user='${{{{ secrets.DB_USER }}}}' \
            -e db_username='${{{{ secrets.DB_USERNAME }}}}' \
            -e db_password='${{{{ secrets.DB_PASSWORD }}}}' \
            -e db_password_plain='${{{{ secrets.DB_PASSWORD }}}}' \
            -e install_db='${{{{ secrets.INSTALL_DB_ON_EC2 }}}}' \
            -e db_type='${{{{ secrets.DB_TYPE }}}}'
"""

def destroy_yml() -> str:
    return f"""\
name: Destroy {APP_NAME} EC2 deployment

on:
  workflow_dispatch:

jobs:
  destroy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Install Ansible
        run: |
          pip install --upgrade pip
          pip install ansible

      - name: Write SSH private key
        run: |
          mkdir -p ~/.ssh
          echo "${{{{ secrets.EC2_SSH_KEY }}}}" > ~/.ssh/deploy_key
          chmod 600 ~/.ssh/deploy_key
          ssh-keyscan -H ${{{{ secrets.EC2_HOST }}}} >> ~/.ssh/known_hosts 2>/dev/null || true

      - name: Write Ansible inventory
        run: |
          mkdir -p ansible/inventory
          cat > ansible/inventory/hosts.ini <<EOF
          [app]
          ${{{{ secrets.EC2_HOST }}}} ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/deploy_key
          EOF

      - name: Stop and remove app
        run: |
          ansible all -i ansible/inventory/hosts.ini -b \
            -m shell \
            -a "systemctl stop {APP_NAME} || true; \
                systemctl disable {APP_NAME} || true; \
                rm -f /etc/systemd/system/{APP_NAME}.service; \
                systemctl daemon-reload; \
                rm -f /etc/nginx/sites-enabled/{APP_NAME}; \
                rm -f /etc/nginx/sites-available/{APP_NAME}; \
                systemctl restart nginx || true; \
                rm -rf /opt/app"
"""

def requirements_txt() -> str:
    return """\
flask>=3.0.0
gunicorn>=21.2.0
uvicorn[standard]>=0.29.0
python-dotenv>=1.0.0
"""

def app_main_py() -> str:
    return """\
import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def index():
    return "Python App is running"

@app.route("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8000))
    app.run(host=host, port=port)
"""

# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n{'='*60}")
    print(f"  Scaffolding Ansible EC2 deployment for {APP_NAME}")
    print(f"  Project : {PROJECT_NAME}")
    print(f"  Region  : {REGION}")
    print(f"  Python  : {PYTHON_VERSION}")
    print(f"  Entry   : {ENTRYPOINT}")
    print(f"{'='*60}\n")

    # ansible.cfg
    write_file(BASE_DIR / "ansible" / "ansible.cfg", ansible_cfg())

    # inventory template
    write_file(INVENTORY_DIR / "hosts.ini.tpl", inventory_template())

    # playbook
    write_file(ANSIBLE_DIR / "playbook.yml", playbook_yml())

    # GitHub Actions workflows
    gha_dir = BASE_DIR / ".github" / "workflows"
    write_file(gha_dir / "deploy.yml",   deploy_yml())
    write_file(gha_dir / "destroy.yml",  destroy_yml())

    # Application stub (only if not already present)
    app_pkg = BASE_DIR / "app"
    req_path = BASE_DIR / "requirements.txt"

    init_path = app_pkg / "__init__.py"
    main_path = app_pkg / "main.py"

    if not init_path.exists():
        write_file(init_path, "")

    if not main_path.exists():
        write_file(main_path, app_main_py())
    else:
        print(f"  [skip]    {main_path}  (already exists)")

    if not req_path.exists():
        write_file(req_path, requirements_txt())
    else:
        print(f"  [skip]    {req_path}  (already exists)")

    print(f"\n{'='*60}")
    print("  Scaffold complete.")
    print(f"{'='*60}\n")
    print("  Required GitHub Secrets:")
    secrets = [
        "EC2_HOST", "EC2_SSH_KEY", "REPO_URL", "GIT_BRANCH",
        "DATABASE_URL", "MONGO_URI",
        "DB_HOST", "DB_PORT", "DB_NAME",
        "DB_USER", "DB_USERNAME", "DB_PASSWORD",
        "INSTALL_DB_ON_EC2", "DB_TYPE",
    ]
    for s in secrets:
        print(f"    • {s}")
    print()

if __name__ == "__main__":
    main()