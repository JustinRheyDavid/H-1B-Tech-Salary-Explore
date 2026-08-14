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

@description('Container image for both the web app and the ETL job. Step 9 replaces this with the real images built from Dockerfile.web and Dockerfile.etl.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

// DEVIATION FROM PLAN STEP 5, which specifies targetPort 8501 while also
// specifying mcr.microsoft.com/k8se/quickstart:latest as the placeholder image.
// Those two are incompatible: the quickstart image serves on port 80, so with
// targetPort 8501 nothing ever accepts a connection, the revision sits in
// `Activating` forever, and requests to the FQDN hang. Step 5's own acceptance
// criterion — "the placeholder web app returns a page over HTTPS" — cannot be
// met with both values as written. Observed exactly that before parameterising.
//
// The port is a property of the image, so it travels with the image rather than
// being hardcoded. Step 9 sets both together:
//
//   --parameters containerImage=ghcr.io/.../h1b-web:latest targetPort=8501
//
// 8501 is Streamlit's default and remains correct for the real image.
@description('Port the web container listens on. Must match containerImage: 80 for the quickstart placeholder, 8501 for the real Streamlit image.')
param targetPort int = 80

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
        // Must match what containerImage actually listens on — see the
        // targetPort parameter. A mismatch does not fail the deployment; the
        // revision reports Succeeded and then hangs in Activating.
        targetPort: targetPort
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
          image: containerImage
          resources: {
            // Container Apps requires memory in Gi and constrains the ratio to
            // 2 Gi per CPU. 0.5 CPU therefore pairs with exactly 1 Gi.
            cpu: json('0.5')
            memory: '1Gi'
          }
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
          image: containerImage
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
