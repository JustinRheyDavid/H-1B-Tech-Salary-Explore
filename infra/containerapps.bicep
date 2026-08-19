// Container Apps environment, the Streamlit web app, and the ETL job.
//
// Cost shape (plan §7): the Consumption plan grants 180,000 vCPU-seconds and
// 2M requests per month free. Two settings keep this at $0.00 and both are
// load-bearing:
//
//   minReplicas: 0   idle costs nothing. Idle charges begin the moment minimum
//                    replicas is greater than zero, so this is the single most
//                    expensive character in the file to change.
//   no VNet          VNet integration and workload profiles both carry an
//                    hourly charge regardless of traffic.
//
// Plan §8 expects a 20-30 second cold start as the consequence of scaling to
// zero. That is the accepted trade, not a bug to fix with minReplicas: 1.

@description('Azure region.')
param location string

@description('Short project prefix. Container app names allow lowercase letters, digits and hyphens.')
param namePrefix string

// The web app and the ETL job take SEPARATE images. They were briefly a single
// parameter, which could not express the required end state: Step 9 pushes
// ghcr.io/.../h-1b-tech-salary-explore-etl:latest while Step 10 builds a
// different image from Dockerfile.web. One parameter would have forced editing
// this file at Step 9 — exactly what parameterising is meant to avoid.
//
// None of the three has a default, deliberately. The web image and its port must
// agree, and a wrong pairing does NOT fail the deployment: ARM reports Succeeded,
// the revision sits in `Activating`, and requests to the FQDN hang with no
// response. That is precisely how plan Step 5's own combination behaves —
// targetPort 8501 against the port-80 quickstart placeholder — and it was
// observed here before this was fixed.
//
// A missing required parameter fails loudly at deploy time and names itself. A
// plausible-looking default fails silently at runtime. Requiring all three means
// Step 9 and Step 10 cannot swap an image while forgetting its port, because
// every value must be stated in infra/main.parameters.json.
@description('Image for the Streamlit web app. Placeholder until Step 10 builds the real one from Dockerfile.web.')
param webImage string

@description('Port the web container listens on. MUST match webImage: 80 for the quickstart placeholder, 8501 for the real Streamlit image.')
param webTargetPort int

@description('Logical SQL server FQDN the dashboard connects to.')
param sqlServerFqdn string

@description('Database name the dashboard connects to.')
param sqlDatabaseName string

@description('Image for the ETL job. Placeholder until Step 9 builds the real one from Dockerfile.etl.')
param etlImage string

// No appLogsConfiguration is declared, so the environment has no log
// destination. This is deliberate: a Log Analytics workspace is billable past
// 5 GB of ingestion and appears nowhere in the plan's §7 cost model, so
// attaching one would breach assumption B3's "no step may create a billable
// resource".
//
// The cost is queryable history — you cannot ask why a container died an hour
// ago. Live streaming from a running container still works and needs no
// workspace:
//
//   az containerapp logs show -n h1b-web -g rg-h1b --follow
//
// If Step 9's ODBC driver install proves hard to debug without retained logs
// (plan §8 rates that risk medium-high), adding a workspace here is a small,
// reversible change.
// What the dashboard needs to find Azure SQL. No secret among them, and none
// possible: the server has azureADOnlyAuthentication, so the h1b-web managed
// identity IS the credential and there is no connection string to leak.
//
// DB_BACKEND is also baked into Dockerfile.web. Declared in both places on
// purpose — the image must be correct when run by hand, and the container app
// must be explicit about what it is running rather than inheriting it.
var webEnvironment = webTargetPort == 8501 ? [
  {
    name: 'DB_BACKEND'
    value: 'azure'
  }
  {
    name: 'AZURE_SQL_SERVER'
    value: sqlServerFqdn
  }
  {
    name: 'AZURE_SQL_DATABASE'
    value: sqlDatabaseName
  }
] : []

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
  properties: {
    zoneRedundant: false
    // Declared because Azure sets them anyway and `what-if` reports undeclared
    // server-set properties as removals on every run. Consumption is the
    // default profile of a consumption-only environment — this is not opting
    // into workload profiles, which plan §7 flags as billable.
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    peerAuthentication: {
      mtls: {
        enabled: false
      }
    }
    peerTrafficConfiguration: {
      encryption: {
        enabled: false
      }
    }
  }
}

// The Streamlit dashboard. Replaces nothing in Phase 1 — the Streamlit Cloud
// deployment stays live per assumption B6.
resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${namePrefix}-web'
  location: location
  // System-assigned identity is how this authenticates to Azure SQL. Step 6
  // grants it db_datareader; it is read-only and has no business writing.
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      ingress: {
        external: true
        // Must match what webImage actually listens on — see the
        // targetPort parameter. A mismatch does not fail the deployment; the
        // revision reports Succeeded and then hangs in Activating.
        targetPort: webTargetPort
        transport: 'auto'
        allowInsecure: false
        // 0 means "not a TCP exposed port". Server-set; declared for what-if.
        exposedPort: 0
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'web'
          image: webImage
          resources: {
            // Container Apps requires memory in Gi and constrains the ratio to
            // 2 Gi per CPU. 0.5 CPU therefore pairs with exactly 1 Gi.
            cpu: json('0.5')
            memory: '1Gi'
          }
          // Step 10. Empty while webImage is the quickstart placeholder, which
          // ignores them; the real image reads both.
          env: webEnvironment
          // Streamlit's own endpoint. Without a readiness probe, Container Apps
          // routes traffic as soon as the process binds the port, which for
          // Streamlit is before the script has run — the first visitor after a
          // scale-from-zero gets a blank page rather than a slow one.
          //
          // Only declared for the real image: the port-80 quickstart has no
          // /_stcore/health, and a probe against it fails every container into
          // a restart loop.
          probes: webTargetPort == 8501 ? [
            {
              type: 'Readiness'
              httpGet: {
                path: '/_stcore/health'
                port: 8501
              }
              // Generous: the container must import pandas and Streamlit, and
              // the first query may be waiting out a serverless resume.
              initialDelaySeconds: 10
              periodSeconds: 10
              failureThreshold: 6
            }
          ] : []
        }
      ]
      scale: {
        // The two values that keep idle free. Do not raise minReplicas.
        minReplicas: 0
        maxReplicas: 1
      }
    }
  }
}

// The ETL job. A Job rather than an app because this is a batch process that
// runs for minutes and exits — it does not serve HTTP.
resource etlJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${namePrefix}-etl'
  location: location
  // Step 6 grants this identity Storage Blob Data Contributor on the storage
  // account and db_datawriter on the database.
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      // Triggered by hand per assumption B7. Step 12 optionally moves this to
      // a schedule.
      //
      // WARNING while etlImage is the quickstart placeholder: that image serves
      // HTTP and never exits, so a manual trigger runs the full replicaTimeout
      // below — one hour at 1 vCPU, 3,600 vCPU-seconds, 2% of the monthly free
      // grant burned for nothing. Do not start this job until Step 9 supplies an
      // image that terminates.
      triggerType: 'Manual'
      // One hour. Loading 850,321 rows with fast_executemany is comfortably
      // inside this; plan §8's fallback is one fiscal year per execution.
      replicaTimeout: 3600
      replicaRetryLimit: 1
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
    }
    template: {
      containers: [
        {
          name: 'etl'
          image: etlImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
        }
      ]
    }
  }
}

output environmentName string = environment.name
output webAppName string = webApp.name
output webAppFqdn string = webApp.properties.configuration.ingress.fqdn
output etlJobName string = etlJob.name

// Consumed by Step 6's role assignments and by the manual T-SQL grants, which
// need the principal IDs to create the database users.
output webPrincipalId string = webApp.identity.principalId
output etlPrincipalId string = etlJob.identity.principalId
