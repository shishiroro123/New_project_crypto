output "instance_id" {
  value       = aws_instance.bot.id
  description = "EC2 instance ID."
}

output "public_ip" {
  value       = aws_eip.bot.public_ip
  description = "Stable public IP. Use this for SSH; it survives stop/start."
}

output "private_key_path" {
  value       = local_sensitive_file.private_key.filename
  description = "Local path of the auto-generated SSH private key."
}

output "ssh_command" {
  value       = "ssh -i ${local_sensitive_file.private_key.filename} ubuntu@${aws_eip.bot.public_ip}"
  description = "Ready-to-paste SSH command."
}

output "dashboard_tunnel" {
  value = format(
    "ssh -i %s -N -L 8501:localhost:8501 ubuntu@%s",
    local_sensitive_file.private_key.filename,
    aws_eip.bot.public_ip,
  )
  description = "SSH tunnel that forwards the bot's Streamlit dashboard to http://localhost:8501."
}

output "next_steps" {
  value       = <<EOT

  -----------------------------------------------------------------
  VM provisioned. The bootstrap script is now installing Docker,
  cloning the repo, and pre-building the image (≈ 2-4 min).

  1. Wait for bootstrap to finish:
        ssh -i ${local_sensitive_file.private_key.filename} ubuntu@${aws_eip.bot.public_ip} 'until [ -f /opt/bootstrap-complete ]; do sleep 5; done && echo OK'

  2. SSH in:
        ssh -i ${local_sensitive_file.private_key.filename} ubuntu@${aws_eip.bot.public_ip}

  3. Add your secrets:
        sudo -u bot vim /opt/crypto-bot/.env

  4. Start the bot (paper trading by default):
        cd /opt/crypto-bot && sudo -u bot docker compose up -d

  5. View dashboard from your laptop (in a separate terminal):
        ssh -i ${local_sensitive_file.private_key.filename} -N -L 8501:localhost:8501 ubuntu@${aws_eip.bot.public_ip}
        # then open http://localhost:8501

  6. Tail the bot logs:
        sudo -u bot docker compose -f /opt/crypto-bot/docker-compose.yml logs -f
  -----------------------------------------------------------------
EOT
  description = "Operator quickstart printed after `terraform apply`."
}
