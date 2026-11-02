data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.public_subnet_cidr
  availability_zone = data.aws_availability_zones.available.names[0]

  tags = { Name = "${var.project}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Only the dashboard port is public. There is no SSH rule: operators use SSM
# Session Manager, and Postgres / MLflow / the API stay on the host's loopback
# or the docker network.
resource "aws_security_group" "app" {
  name        = "${var.project}-app"
  description = "PMI platform host"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-app" }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each = toset(var.http_ingress_cidrs)

  security_group_id = aws_security_group.app.id
  description       = "Dashboard (HTTP)"
  cidr_ipv4         = each.value
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.app.id
  description       = "Package installs, image pulls, LLM APIs, Slack"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
