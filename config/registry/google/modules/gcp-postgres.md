---
title: "postgres"
linkTitle: "postgres"
date: 2021-07-21
draft: false
weight: 1
description: Creates a postgres (GCP Cloud SQL) database instance
---

This module creates a postgres [GCP Cloud SQL](https://cloud.google.com/sql/docs/introduction) database. It is made with
the [private service access](https://cloud.google.com/vpc/docs/private-services-access), ensuring private communication.

### Linking

When linked to a k8s-service, it adds connection credentials to your container's environment variables as:

- `{module_name}_db_user`
- `{module_name}_db_password`
- `{module_name}_db_name`
- `{module_name}_db_host`

In the [modules reference](/reference) example, the _{module_name}_ would be replaced with `rds`

The permission list can optionally have one entry which should be a map for renaming the default environment variable
names to a user-defined value:

```yaml
links:
  - db:
      - db_user: DBUSER
        db_host: DBHOST
        db_name: DBNAME
        db_password: DBPASS
```

If present, this map must have renames for all 4 fields.
