variable "aws_region" {
  type        = string
  default     = "eu-west-3"
  description = "AWS region. eu-west-3 = Paris."
}

variable "project_name" {
  type        = string
  default     = "crypto-bot"
  description = "Tag prefix for every resource."
}

variable "instance_type" {
  type        = string
  default     = "t4g.small"
  description = <<EOT
EC2 instance class. t4g.small = 2 vCPU ARM, 2 GB RAM, ~$14/mo in Paris.
Cheapest reliable option for our workload (bot + dashboard + occasional backtest).
Use t3.small for x86 if you need to.
EOT
}

variable "volume_size_gb" {
  type        = number
  default     = 16
  description = "Root EBS size. 16 GB is plenty for OS + Docker images + parquet cache."
}

variable "allowed_ssh_cidr" {
  type        = string
  default     = "0.0.0.0/0"
  description = <<EOT
CIDR block allowed to reach SSH. Default is wide open but the security
group only accepts the auto-generated SSH key (no password auth).
For tighter security, set this to YOUR_IP/32:
  terraform apply -var allowed_ssh_cidr=$(curl -s ifconfig.me)/32
EOT
}

variable "repo_url" {
  type        = string
  default     = "https://github.com/shirawww-debug/New_project_crypto.git"
  description = "Git URL of the bot repo. Must be reachable from the VM."
}

variable "repo_branch" {
  type        = string
  default     = "claude/trading-bot-design-9hJLr"
  description = "Branch to check out on the VM."
}

variable "vpc_cidr" {
  type        = string
  default     = "10.42.0.0/16"
  description = "CIDR of the VPC we create. Override if it collides with an existing peering."
}

variable "subnet_cidr" {
  type        = string
  default     = "10.42.1.0/24"
  description = "CIDR of the public subnet."
}
