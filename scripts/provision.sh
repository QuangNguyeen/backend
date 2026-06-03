#!/usr/bin/env bash
# DictaLearn — one-time VPS provisioning (run as root on a fresh Ubuntu 24.04 VPS).
#   ssh root@<IP_VPS> 'bash -s' < scripts/provision.sh
set -e

# 1. Non-root sudo user
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh

# 2. Docker + compose plugin
curl -fsSL https://get.docker.com | sh
usermod -aG docker deploy

# 3. Firewall
ufw allow 22 && ufw allow 80 && ufw allow 443
ufw --force enable

# 4. Swap 2 GB (safety margin during transcription)
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile
    mkswap /swapfile && swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "Provision done. Re-login as user 'deploy' and clone the repo into ~/app."