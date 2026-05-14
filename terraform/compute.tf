# Auto-generate an SSH keypair and write the private half locally with
# 0600 perms. Saves the user from having to manage one — the deploy is
# zero-prep beyond `terraform apply`.

resource "tls_private_key" "ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "main" {
  key_name   = "${var.project_name}-key"
  public_key = tls_private_key.ssh.public_key_openssh

  tags = {
    Name = "${var.project_name}-key"
  }
}

resource "local_sensitive_file" "private_key" {
  filename        = "${path.module}/crypto-bot.pem"
  content         = tls_private_key.ssh.private_key_pem
  file_permission = "0600"
}

# Security group: SSH in (key-only, no password), all egress out.
# Port 8501 (Streamlit dashboard) is intentionally NOT exposed — access
# it via SSH tunnel instead, which keeps the dashboard private.

resource "aws_security_group" "bot" {
  name        = "${var.project_name}-sg"
  description = "Crypto-bot VM: SSH ingress only, all egress."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project_name}-sg"
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.bot.id
  description       = "SSH"
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
  cidr_ipv4         = var.allowed_ssh_cidr
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.bot.id
  description       = "All outbound"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

# Optional: open the Streamlit dashboard (port 8501) to a specific CIDR.
# Only created when var.allowed_dashboard_cidr is non-empty.
resource "aws_vpc_security_group_ingress_rule" "dashboard" {
  count             = var.allowed_dashboard_cidr != "" ? 1 : 0
  security_group_id = aws_security_group.bot.id
  description       = "Streamlit dashboard"
  ip_protocol       = "tcp"
  from_port         = 8501
  to_port           = 8501
  cidr_ipv4         = var.allowed_dashboard_cidr
}

# The VM itself.
resource "aws_instance" "bot" {
  ami                    = data.aws_ami.ubuntu_arm.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.bot.id]
  key_name               = aws_key_pair.main.key_name

  user_data = templatefile("${path.module}/user_data.sh", {
    repo_url    = var.repo_url
    repo_branch = var.repo_branch
  })
  # Don't reboot the VM if user_data changes — bootstrap is idempotent and
  # we'd rather leave a healthy bot running than churn it.
  user_data_replace_on_change = false

  # IMDSv2 required → blocks SSRF-style metadata exfiltration.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.volume_size_gb
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-root"
    }
  }

  tags = {
    Name = "${var.project_name}-vm"
  }

  lifecycle {
    # Resizing or AMI changes would normally destroy + recreate the VM,
    # which means losing local SQLite state. Make it explicit.
    ignore_changes = [ami]
  }
}
