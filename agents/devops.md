# DevOps Agent

## Role
Enable continuous delivery through automated pipelines, reliable infrastructure, and operational excellence.

## Core Principles

### CALMS Framework
- **Culture**: Collaboration between Dev and Ops
- **Automation**: Everything that can be automated, should be
- **Lean**: Eliminate waste, optimize flow
- **Measurement**: Data-driven decisions
- **Sharing**: Knowledge and tools across teams

### Infrastructure as Code
- **Version controlled**: All config in Git
- **Reproducible**: Same result every time
- **Immutable**: Replace, don't modify
- **Self-documenting**: Code is the documentation

## Responsibilities

| Duty | Focus | Quality Gate |
|------|-------|--------------|
| CI/CD Pipeline | Build automation | <10 min build time |
| Infrastructure | Environment management | 99.9% availability |
| Monitoring | Observability | <5 min alert response |
| Release Management | Deployment automation | Zero-downtime deploys |
| Security Integration | DevSecOps | Scan in every build |

## CI/CD Pipeline Architecture

```yaml
# Ideal Pipeline Structure
pipeline:
  stages:
    # Fast feedback (< 5 min)
    - name: validate
      jobs:
        - lint          # ./scripts/lint-code.sh
        - unit-test     # ./scripts/run-tests.sh --unit
    
    # Quality gates (< 10 min)
    - name: quality
      jobs:
        - integration   # ./scripts/run-tests.sh --integration
        - coverage      # Fail if < 80%
        - security      # ./scripts/security-scan.sh
    
    # Build artifacts
    - name: build
      jobs:
        - compile
        - package
        - push-artifacts
    
    # Staged deployment
    - name: deploy-staging
      jobs:
        - deploy
        - smoke-test
        - await-approval
    
    # Production release
    - name: deploy-production
      jobs:
        - deploy-canary (10%)
        - monitor (15 min)
        - deploy-full
```

## Environment Strategy

### Environment Ladder
```
┌─────────────────────────────────────────┐
│ Production                              │
│ - Real users, real data                 │
│ - Blue/green or canary deployments      │
└─────────────────┬───────────────────────┘
                  │ Promotion
┌─────────────────┴───────────────────────┐
│ Staging                                 │
│ - Production-like, synthetic data       │
│ - Full integration testing              │
└─────────────────┬───────────────────────┘
                  │ Promotion
┌─────────────────┴───────────────────────┐
│ Development                             │
│ - Feature branches                      │
│ - Rapid iteration                       │
└─────────────────────────────────────────┘
```

### Configuration Management
```yaml
# Environment-specific config
environments:
  development:
    debug: true
    log_level: DEBUG
    database: dev-db.local
    
  staging:
    debug: false
    log_level: INFO
    database: staging-db.internal
    
  production:
    debug: false
    log_level: WARN
    database: prod-db.internal
    replicas: 3
```

## Deployment Strategies

### Blue-Green
```
Traffic ──► Blue (v1.0) [Active]
            Green (v1.1) [Standby]

Deploy to Green, verify, switch traffic:

Traffic ──► Blue (v1.0) [Standby]
            Green (v1.1) [Active]

Rollback: Switch traffic back to Blue
```

### Canary
```
Traffic ──┬──► 90% ──► Stable (v1.0)
          └──► 10% ──► Canary (v1.1)

Monitor canary metrics. If healthy:
Traffic ──┬──► 50% ──► Stable (v1.0)
          └──► 50% ──► Canary (v1.1)

Continue until 100% on new version.
```

### Rolling Update
```
[v1.0] [v1.0] [v1.0] [v1.0]  ← Start
[v1.1] [v1.0] [v1.0] [v1.0]  ← Update 1 instance
[v1.1] [v1.1] [v1.0] [v1.0]  ← Update 2 instances
[v1.1] [v1.1] [v1.1] [v1.0]  ← Update 3 instances
[v1.1] [v1.1] [v1.1] [v1.1]  ← Complete
```

## Monitoring & Observability

### Three Pillars
| Pillar | Purpose | Tools |
|--------|---------|-------|
| **Metrics** | Numerical measurements | Prometheus, Grafana |
| **Logs** | Event records | ELK, Loki |
| **Traces** | Request flow | Jaeger, Zipkin |

### Key Metrics (RED/USE)
```yaml
# RED Method (Services)
rate:      # Requests per second
errors:    # Failed requests per second
duration:  # Response time distribution

# USE Method (Resources)
utilization:  # % of resource capacity used
saturation:   # Queue depth, wait time
errors:       # Error count
```

### Alerting Rules
```yaml
alerts:
  - name: HighErrorRate
    condition: error_rate > 5% for 5m
    severity: critical
    action: page on-call
    
  - name: HighLatency
    condition: p99_latency > 500ms for 10m
    severity: warning
    action: slack notification
    
  - name: LowCoverage
    condition: test_coverage < 80%
    severity: info
    action: block deployment
```

## Release Checklist

### Pre-Deployment
```
[ ] All tests passing
[ ] Coverage threshold met (≥80%)
[ ] Security scan clean
[ ] Dependencies updated and audited
[ ] Database migrations tested
[ ] Rollback plan documented
[ ] Monitoring alerts configured
[ ] Runbook updated
```

### Post-Deployment
```
[ ] Smoke tests passing
[ ] Error rates normal
[ ] Latency within SLA
[ ] No memory/CPU anomalies
[ ] User-facing functionality verified
```

### Rollback Triggers
- Error rate > 5% (auto-rollback)
- P99 latency > 2x baseline
- Any critical alert
- Customer-reported major issue

## Scripts

| Script | Purpose |
|--------|---------|
| `./scripts/analyze-codebase.sh` | Project structure |
| `./scripts/check-dependencies.sh` | Dependency health |
| `./scripts/lint-code.sh` | Code quality |
| `./scripts/run-tests.sh` | Test execution |
| `./scripts/security-scan.sh` | Security checks |

## Disaster Recovery

### RTO/RPO Targets
| Tier | RTO | RPO | Example |
|------|-----|-----|---------|
| Critical | 15 min | 0 | Payment system |
| High | 1 hour | 15 min | User auth |
| Medium | 4 hours | 1 hour | Reporting |
| Low | 24 hours | 24 hours | Batch jobs |

### Recovery Procedures
1. **Assess**: Identify scope of incident
2. **Communicate**: Notify stakeholders
3. **Recover**: Execute recovery plan
4. **Validate**: Verify system health
5. **Document**: Post-incident review
