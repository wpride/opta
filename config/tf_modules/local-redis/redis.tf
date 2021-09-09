provider "helm" {
  kubernetes {
    config_path="~/.opta/kind/config"
  }
}

resource "helm_release" "opta-local-redis" {
  name       = "opta-local-redis"
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "redis"
  version    = "15.3.2"

  set {
    name  = "cluster.enabled"
    value = "false"
  }
  set {
    name  = "auth.enabled"
    value = "false"
  }

  set {
    name  = "metrics.enabled"
    value = "false"
  }
  set {
    name = "architecture"
    value = "standalone"
}
}


