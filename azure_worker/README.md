# OppFindAgent five-minute scheduler (Azure Functions)

This small timer app calls the existing protected Vercel worker endpoint. It does not store user data or Notion credentials; the application and Supabase database remain on Vercel/Supabase.

## Why this exists

Vercel Hobby Cron is daily-only per cron entry and its execution can drift by up to 59 minutes. Azure Functions Timer Trigger supports a five-minute NCRONTAB schedule. The timer checks the user's saved local-time schedule and asks the production app to process one due user. The app's database lease prevents duplicate work.

## Create the Function App

1. Activate Azure for Students using your university identity. The current student offer includes USD $100 Azure credit for 12 months, with no credit card required for eligible students.
2. In the Azure portal, create a Function App:
   - Publish: Code
   - Runtime stack: Python
   - Version: Python 3.11 (or the latest Python version supported by the selected Functions runtime)
   - Operating system: Linux
   - Hosting: Consumption
   - Create/select a Storage account for AzureWebJobsStorage
3. In Function App → Settings → Environment variables, add:
   - OPPFINDAGENT_WORKER_URL=https://research-opportunity-agent.vercel.app/api/cron/worker
   - OPPFINDAGENT_CRON_SECRET: the exact existing CRON_SECRET value from Vercel Production.
4. Deploy this folder as the Function App project. From this folder, use Azure Functions Core Tools:
   func azure functionapp publish YOUR_FUNCTION_APP_NAME
5. In Function App → Functions, confirm oppfindagent_scheduler is enabled. In Monitor / Application Insights, confirm it runs every five minutes and returns HTTP 200 from the Vercel worker.
6. Only after the five-minute timer has run successfully for at least a day, remove the 24 hourly fallback cron entries from the root vercel.json. Keep the protected Vercel endpoint.

## Security and operation

- Never commit CRON_SECRET, Azure publish profiles, or storage connection strings.
- The timer sends only the worker URL and bearer secret; it does not need Gmail, Notion, Tavily, or database credentials.
- The worker endpoint claims at most one due user per tick. If a job fails, the app records the failure and a later tick can retry according to the job lease.
- The schedule is UTC and checks every five minutes. Each user's target time is still interpreted using their saved IANA timezone. Expected pickup is within roughly five minutes, subject to cloud cold starts and platform delays; it is not a hard real-time guarantee.
- Azure Functions Consumption includes a monthly execution grant, but the required Storage account and other ancillary Azure resources can incur separate charges. Monitor Azure Cost Management and set a budget alert before scaling.
