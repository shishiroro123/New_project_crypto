# Terraform — déploiement AWS

Provisionne en 3 commandes une VM EC2 prête à faire tourner le bot 24/7.

## Ce qui est créé

| Ressource | Détail |
|---|---|
| VPC + subnet public + IGW + route table | Isolation réseau dédiée (10.42.0.0/16) |
| Security group | SSH (22) en ingress, tout en egress. Port 8501 (dashboard) **non exposé** |
| Elastic IP | IP publique stable, survit aux stop/start |
| Key pair (auto-générée) | Clé RSA 4096 bits, écrite localement en `crypto-bot.pem` (mode 600) |
| EC2 t4g.small (ARM, 2 vCPU, 2 GB) | Ubuntu 24.04 LTS ARM |
| EBS root 16 GB gp3 chiffré | OS + Docker images + cache parquet |
| User-data bootstrap | Installe Docker, clone le repo, pré-build l'image |

**Coût estimé** : ~$15/mois (t4g.small Paris ≈ $13.72 + EBS 16 GB ≈ $1.28). Pris sur tes crédits AWS.

## Pré-requis

1. **AWS CLI configuré** : `aws configure` avec une clé qui peut créer EC2/VPC/EIP. Vérifier :
   ```bash
   aws sts get-caller-identity
   ```
2. **Terraform ≥ 1.5** : `brew install terraform` (mac) ou `apt-get install terraform` (Linux).
3. ~5 minutes.

## Déploiement (chemin par défaut)

```bash
cd terraform/
terraform init      # télécharge les providers AWS / TLS / local
terraform plan      # check qu'on va bien créer ce qu'on veut (optional)
terraform apply     # tape "yes" — création en ~3 min
```

Outputs imprimés :
- `public_ip` : l'IP de la VM
- `ssh_command` : commande prête à coller
- `dashboard_tunnel` : tunnel SSH vers le dashboard
- `next_steps` : guide pas-à-pas

## Premier démarrage du bot

Toutes les commandes ci-dessous sont copiables depuis `terraform output next_steps`.

```bash
# 1. Attendre la fin du bootstrap (Docker install + clone + build).
#    ~3-4 min après apply. Le marqueur /opt/bootstrap-complete apparait quand c'est OK.
ssh -i crypto-bot.pem ubuntu@<public_ip> 'until [ -f /opt/bootstrap-complete ]; do sleep 5; done && echo OK'

# 2. SSH en interactif
ssh -i crypto-bot.pem ubuntu@<public_ip>

# 3. Sur la VM, configurer les secrets (Binance + Telegram + ca_bundle si besoin)
sudo -u bot vim /opt/crypto-bot/.env

# 4. Démarrer le bot (paper trading par défaut)
cd /opt/crypto-bot && sudo -u bot docker compose up -d

# 5. Suivre les logs
sudo -u bot docker compose logs -f

# 6. Status quand tu veux
sudo -u bot docker compose exec bot crypto-bot status
```

## Dashboard Streamlit (depuis ta machine)

Le dashboard n'est PAS exposé publiquement. On y accède via tunnel SSH :

```bash
# Dans un terminal local
ssh -i crypto-bot.pem -N -L 8501:localhost:8501 ubuntu@<public_ip>

# Dans un autre terminal, sur la VM, démarrer le dashboard si pas déjà
ssh -i crypto-bot.pem ubuntu@<public_ip>
sudo -u bot docker compose run --rm -p 127.0.0.1:8501:8501 bot dashboard --host 127.0.0.1
```

Puis ouvrir <http://localhost:8501> dans ton navigateur.

## Restreindre SSH à ton IP (recommandé une fois confortable)

Par défaut SSH est ouvert à `0.0.0.0/0` (clé only). Pour restreindre :

```bash
# Ton IP publique courante
MY_IP=$(curl -s ifconfig.me)

# Re-applique avec la restriction
terraform apply -var "allowed_ssh_cidr=${MY_IP}/32"
```

## Mises à jour du code sur la VM

```bash
ssh -i crypto-bot.pem ubuntu@<public_ip>
cd /opt/crypto-bot
sudo -u bot git pull
sudo -u bot docker compose build
sudo -u bot docker compose up -d
```

Si tu changes de branche : `sudo -u bot git checkout <branch>` puis `git pull`.

## Variables disponibles

Toutes optionnelles, défauts dans `variables.tf` :

| Variable | Défaut | Description |
|---|---|---|
| `aws_region` | `eu-west-3` | Région AWS |
| `instance_type` | `t4g.small` | Classe EC2 |
| `volume_size_gb` | `16` | Taille root EBS |
| `allowed_ssh_cidr` | `0.0.0.0/0` | IP autorisée à SSH |
| `repo_url` | `github.com/shirawww-debug/New_project_crypto.git` | Repo à cloner |
| `repo_branch` | `claude/trading-bot-design-9hJLr` | Branche |
| `vpc_cidr` | `10.42.0.0/16` | CIDR VPC |
| `subnet_cidr` | `10.42.1.0/24` | CIDR subnet public |

Override via `-var` :
```bash
terraform apply -var "instance_type=t3.small" -var "aws_region=eu-central-1"
```

Ou créer un `terraform.tfvars` (gitignored) :
```hcl
aws_region       = "eu-central-1"
instance_type    = "t3.small"
allowed_ssh_cidr = "1.2.3.4/32"
```

## Destruction

```bash
terraform destroy
```

Tape `yes`. Tout est nettoyé : VM, EIP, EBS, VPC, key pair. La clé locale `crypto-bot.pem` reste sur ton disque (à supprimer manuellement si tu veux).

> ⚠️ Le state SQLite + parquet cache de la VM **disparaissent** avec la destruction (EBS root supprimé). Si le bot a accumulé de l'historique de trades important : `scp` le `data/state.sqlite` vers ton local AVANT de détruire.

## Sécurité — checklist live

Avant de mettre du capital réel sur le bot tournant sur cette VM :

- [ ] Restreindre `allowed_ssh_cidr` à ton IP fixe (ou un VPN)
- [ ] Activer la 2FA sur ton compte AWS root
- [ ] Créer une IAM user dédiée à Terraform (pas la root key)
- [ ] Clés API Binance : permissions read + spot uniquement, **withdrawals désactivés**, IP whitelist = `<public_ip>` de la VM
- [ ] Configurer un budget AWS alert (CloudWatch billing) pour éviter les surprises
- [ ] Backup périodique du `data/state.sqlite` (cron + S3)

## Que faire si ça plante

| Symptôme | Diagnostic |
|---|---|
| `terraform apply` échoue sur AMI | La région n'a pas l'image ARM64 demandée → essayer `instance_type=t3.small` (x86) |
| Bootstrap pas terminé après 10 min | `ssh ... cat /var/log/bootstrap.log` pour voir où ça coince |
| `docker compose build` lent ou OOM | Trop léger ; switch vers `instance_type=t3.medium` |
| Bot crashe au démarrage | `.env` mal rempli, vérifier les variables `BINANCE_*` et `EXCHANGE_NAME` |
| SSH timeout | Vérifier `allowed_ssh_cidr` ou ton IP publique a changé |
