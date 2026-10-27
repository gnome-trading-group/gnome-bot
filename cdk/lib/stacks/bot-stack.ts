import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { Platform } from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as lambdaEventSources from 'aws-cdk-lib/aws-lambda-event-sources';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { join } from 'path';
import { CallerKind, GnomeAccount, GnomeApiAuthorizer, Stage } from '@gnome-trading-group/gnome-shared-cdk';
import { PIPELINES_READ_ROLE_NAME, WORKER_ROLE_NAME } from '../config';

interface Props extends cdk.StackProps {
  stage: Stage;
}

// Slack events come in through a public API (Slack signs them), wait on a queue, and are answered by the worker,
// which reads the system through the registry, controller and launcher APIs as the bot's Cognito client, and through
// read-only AWS calls in its own account and the pipelines account.
export class BotStack extends cdk.Stack {
  public readonly ingressFn: lambda.DockerImageFunction;
  public readonly workerFn: lambda.DockerImageFunction;
  public readonly workerLogGroup: logs.LogGroup;
  public readonly interactFn: lambda.DockerImageFunction;
  public readonly callbackFn: lambda.DockerImageFunction;
  public readonly webhookFn: lambda.DockerImageFunction;
  public readonly notesFn: lambda.DockerImageFunction;
  public readonly queue: sqs.Queue;
  public readonly dlq: sqs.Queue;

  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const imageAsset = join(__dirname, '..', '..', '..');

    const table = new dynamodb.Table(this, 'BotTable', {
      tableName: 'gnome-bot',
      partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
    });

    this.dlq = new sqs.Queue(this, 'WorkDlq', { retentionPeriod: cdk.Duration.days(14) });
    // The worker answers failures in the thread rather than raising, so a redelivery means it died outright.
    this.queue = new sqs.Queue(this, 'WorkQueue', {
      visibilityTimeout: cdk.Duration.minutes(6),
      deadLetterQueue: { queue: this.dlq, maxReceiveCount: 2 },
    });

    // The registry's alert router announces each firing alert it posts to Slack on its bus; the worker triages it.
    // The bus is named rather than imported, like every other stack that puts events on it.
    new events.Rule(this, 'AlertDeliveredRule', {
      description: 'Alerts posted to Slack go to gnome-bot for triage',
      eventBus: events.EventBus.fromEventBusName(this, 'AlertBus', 'gnome-alerts'),
      eventPattern: { source: ['gnome.alerts.delivered'], detailType: ['AlertDelivered'] },
      targets: [new targets.SqsQueue(this.queue)],
    });

    // Made before the functions so they can be told the callback URL. The URL is built from the API's id rather than
    // its deployment, which depends on every method and so on the functions behind them.
    const api = new apigateway.RestApi(this, 'BotApi', {
      restApiName: 'gnome-bot-api',
      deployOptions: { stageName: 'api' },
    });
    const callbackUrl = `https://${api.restApiId}.execute-api.${this.region}.${this.urlSuffix}/api/github/callback`;

    const pipelinesReadRoleArn =
      `arn:aws:iam::${GnomeAccount.InfraPipelines.accountId}:role/${PIPELINES_READ_ROLE_NAME}`;
    const registryApiKeyId = cdk.Fn.importValue('RegistryApiKeyId');

    const environment = {
      STAGE: props.stage,
      BOT_TABLE: table.tableName,
      WORK_QUEUE_URL: this.queue.queueUrl,
      REGISTRY_API_URL: cdk.Fn.importValue('RegistryApiUrl'),
      CONTROLLER_API_URL: cdk.Fn.importValue('ControllerApiUrl'),
      LAUNCHER_API_URL: cdk.Fn.importValue('LauncherApiUrl'),
      // For gnomepy's send_alert, which posts the bot's own alerts with the registry's API key.
      REGISTRY_API_KEY_ID: registryApiKeyId,
      PIPELINES_READ_ROLE_ARN: pipelinesReadRoleArn,
      CALLBACK_URL: callbackUrl,
    };

    const image = (cmd: string) => lambda.DockerImageCode.fromImageAsset(imageAsset, {
      cmd: [cmd],
      // The functions run on x86_64; pinned so an image built on an arm64 machine still runs.
      platform: Platform.LINUX_AMD64,
    });

    this.ingressFn = new lambda.DockerImageFunction(this, 'SlackEvents', {
      code: image('bot.handlers.slack_events.handler'),
      // Slack retries anything slower than three seconds.
      timeout: cdk.Duration.seconds(10),
      memorySize: 512,
      environment,
    });
    table.grantReadWriteData(this.ingressFn);
    this.queue.grantSendMessages(this.ingressFn);
    this.ingressFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['secretsmanager:GetSecretValue'],
      resources: [`arn:aws:secretsmanager:${this.region}:${this.account}:secret:slack-signing-secret*`],
    }));

    const workerRole = new iam.Role(this, 'WorkerRole', {
      roleName: WORKER_ROLE_NAME,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole'),
        // The aws_read tool reads anything in the account (cost, CloudTrail, every resource's state), as does
        // everything else the bot already looks at here: logs, stacks, the API key send_alert uses.
        iam.ManagedPolicy.fromAwsManagedPolicyName('ReadOnlyAccess'),
      ],
    });
    // Made here rather than left to Lambda's first invocation, so the monitoring stack's metric filter has a log group
    // to attach to on the first deploy.
    this.workerLogGroup = new logs.LogGroup(this, 'WorkerLogs', { retention: logs.RetentionDays.ONE_MONTH });
    this.workerFn = new lambda.DockerImageFunction(this, 'Worker', {
      code: image('bot.handlers.worker.handler'),
      timeout: cdk.Duration.minutes(5),
      memorySize: 1024,
      role: workerRole,
      logGroup: this.workerLogGroup,
      environment,
    });
    this.workerFn.addEventSource(new lambdaEventSources.SqsEventSource(this.queue, { batchSize: 1 }));
    table.grantReadWriteData(this.workerFn);

    // Whatever the worker reads can end up in a Slack message, so secret values stay closed even if ReadOnlyAccess
    // grows: only the bot's own secrets and the one parameter it needs. KMS is left alone: Lambda decrypts the
    // function's environment with this role, as a direct kms:Decrypt call, so denying those stops the worker starting.
    const botSecretArns = [
      `arn:aws:secretsmanager:${this.region}:${this.account}:secret:slack-bot-token*`,
      `arn:aws:secretsmanager:${this.region}:${this.account}:secret:gnome-bot/*`,
    ];
    const alertingUrlParameter = `arn:aws:ssm:${this.region}:${this.account}:parameter/gnome/alerting/api-url`;
    const denials = [
      {
        effect: iam.Effect.DENY,
        actions: ['secretsmanager:GetSecretValue', 'secretsmanager:BatchGetSecretValue'],
        notResources: botSecretArns,
      },
      {
        effect: iam.Effect.DENY,
        actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath', 'ssm:GetParameterHistory'],
        notResources: [alertingUrlParameter],
      },
    ];
    for (const statement of denials) {
      this.workerFn.addToRolePolicy(new iam.PolicyStatement(statement));
    }

    // Reads beyond ReadOnlyAccess, and the model calls.
    const statements = [
      { actions: ['secretsmanager:GetSecretValue'], resources: botSecretArns },
      { actions: ['sts:AssumeRole'], resources: [pipelinesReadRoleArn] },
      { actions: ['ssm:GetParameter'], resources: [alertingUrlParameter] },
      // For the `bedrock` provider, switchable in the service config without a deploy.
      {
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          'arn:aws:bedrock:*::foundation-model/anthropic.*',
          `arn:aws:bedrock:*:${this.account}:inference-profile/*anthropic.*`,
        ],
      },
    ];
    for (const statement of statements) {
      this.workerFn.addToRolePolicy(new iam.PolicyStatement(statement));
    }

    const botSecrets = new iam.PolicyStatement({ actions: ['secretsmanager:GetSecretValue'], resources: botSecretArns });

    // Gnome Bot's Slack buttons, forwarded by the launcher (which owns the Slack app's one Interactivity URL) as an
    // async invoke. Its name is fixed so the launcher can grant and invoke it without importing anything from here.
    this.interactFn = new lambda.DockerImageFunction(this, 'Interact', {
      functionName: 'gnome-bot-interact',
      code: image('bot.handlers.interact.handler'),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment,
    });
    table.grantReadWriteData(this.interactFn);
    this.interactFn.addToRolePolicy(botSecrets);

    // Where a fix run reports its result; it authenticates with its GitHub OIDC token, checked in the handler.
    this.callbackFn = new lambda.DockerImageFunction(this, 'GithubCallback', {
      code: image('bot.handlers.github_callback.handler'),
      timeout: cdk.Duration.seconds(15),
      memorySize: 512,
      environment,
    });
    table.grantReadWriteData(this.callbackFn);
    this.callbackFn.addToRolePolicy(botSecrets);

    // The GitHub App's webhook (PR comments mentioning @gnome-bot). GitHub signs each delivery with this secret, which
    // is pasted into the App's settings along with the URL; only prod's is configured, since an App has one webhook.
    const webhookSecret = new secretsmanager.Secret(this, 'GithubWebhookSecret', {
      secretName: 'gnome-bot/github-webhook-secret',
      description: "Secret the gnome-trading-bot GitHub App signs its webhook deliveries with",
      generateSecretString: { passwordLength: 48, excludePunctuation: true },
    });
    this.webhookFn = new lambda.DockerImageFunction(this, 'GithubWebhook', {
      code: image('bot.handlers.github_webhook.handler'),
      // GitHub gives a delivery 10 seconds.
      timeout: cdk.Duration.seconds(10),
      memorySize: 512,
      environment,
    });
    webhookSecret.grantRead(this.webhookFn);
    this.queue.grantSendMessages(this.webhookFn);

    // Team notes for the controller's Tools → Bot Notes page: people only, with their Cognito ID token, through the
    // same shared authorizer as every other gnome API. The routes are CORS-enabled for the browser; the rest aren't.
    this.notesFn = new lambda.DockerImageFunction(this, 'Notes', {
      code: image('bot.handlers.notes.handler'),
      timeout: cdk.Duration.seconds(15),
      memorySize: 512,
      environment,
    });
    table.grantReadWriteData(this.notesFn);
    const userPool = cognito.UserPool.fromUserPoolArn(this, 'OperatorUserPool',
      ssm.StringParameter.valueForStringParameter(this, '/gnome/cognito/user-pool-arn'));
    const auth = new GnomeApiAuthorizer(this, 'Authorizer', { userPool });
    // Without these, a request the authorizer refuses comes back without CORS headers and the browser reports an
    // opaque CORS failure instead of the auth error.
    api.addGatewayResponse('Default4xxCors', {
      type: apigateway.ResponseType.DEFAULT_4XX,
      responseHeaders: { 'Access-Control-Allow-Origin': "'*'" },
    });
    api.addGatewayResponse('Default5xxCors', {
      type: apigateway.ResponseType.DEFAULT_5XX,
      responseHeaders: { 'Access-Control-Allow-Origin': "'*'" },
    });
    const notes = api.root.addResource('notes', {
      defaultCorsPreflightOptions: {
        allowOrigins: apigateway.Cors.ALL_ORIGINS,
        allowMethods: ['GET', 'PATCH', 'DELETE', 'OPTIONS'],
        allowHeaders: apigateway.Cors.DEFAULT_HEADERS,
      },
    });
    const notesIntegration = new apigateway.LambdaIntegration(this.notesFn);
    const people: apigateway.MethodOptions = {
      authorizer: auth.authorizerFor([CallerKind.PERSON]),
      authorizationType: apigateway.AuthorizationType.CUSTOM,
    };
    notes.addMethod('GET', notesIntegration, people);
    const note = notes.addResource('{id}');
    note.addMethod('PATCH', notesIntegration, people);
    note.addMethod('DELETE', notesIntegration, people);

    api.root.resourceForPath('slack/events').addMethod('POST', new apigateway.LambdaIntegration(this.ingressFn));
    api.root.resourceForPath('github/webhook').addMethod('POST', new apigateway.LambdaIntegration(this.webhookFn));
    api.root.resourceForPath('github/callback').addMethod('POST', new apigateway.LambdaIntegration(this.callbackFn));

    new cdk.CfnOutput(this, 'GithubWebhookUrl', {
      value: `${api.url}github/webhook`,
      description: "The gnome-trading-bot GitHub App's Webhook URL (prod), with the secret gnome-bot/github-webhook-secret",
    });
    new cdk.CfnOutput(this, 'BotApiUrl', {
      value: api.url,
      description: "The controller's VITE_BOT_API_URL (Tools → Bot Notes)",
    });
    new cdk.CfnOutput(this, 'SlackEventsUrl', {
      value: `${api.url}slack/events`,
      description: "Set this as the Event Subscriptions Request URL in the Gnome Bot Slack app",
    });
  }
}
