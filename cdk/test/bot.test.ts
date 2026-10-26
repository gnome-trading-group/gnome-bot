import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { GnomeAccount, Stage } from '@gnome-trading-group/gnome-shared-cdk';
import { BotPipelineStack } from '../lib/bot-pipeline-stack';
import { BotStack } from '../lib/stacks/bot-stack';
import { BotMonitoringStack } from '../lib/stacks/monitoring-stack';

// Skips building the Docker image so the tests only check the wiring.
const app = () => new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });

describe('BotStack', () => {
  const scope = app();
  const stack = new BotStack(scope, 'Bot', { env: GnomeAccount.InfraDev.environment, stage: Stage.DEV });
  const monitoring = new BotMonitoringStack(scope, 'BotMonitoring', {
    env: GnomeAccount.InfraDev.environment,
    ingressFn: stack.ingressFn, workerFn: stack.workerFn, workerLogGroup: stack.workerLogGroup, queue: stack.queue,
    dlq: stack.dlq, interactFn: stack.interactFn, callbackFn: stack.callbackFn, webhookFn: stack.webhookFn,
  });
  const template = Template.fromStack(stack);

  it('takes Slack events, fix-run callbacks and the GitHub webhook, each on one public POST route', () => {
    const methods = Object.values(template.findResources('AWS::ApiGateway::Method')).map((m) => m.Properties.HttpMethod);
    expect(methods).toEqual(['POST', 'POST', 'POST']);
    expect(Object.values(template.findResources('AWS::ApiGateway::Resource')).map((r) => r.Properties.PathPart).sort())
      .toEqual(['callback', 'events', 'github', 'slack', 'webhook']);
  });

  it('generates the webhook secret GitHub signs deliveries with', () => {
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: 'gnome-bot/github-webhook-secret',
      GenerateSecretString: Match.objectLike({ PasswordLength: 48 }),
    });
  });

  it('names the interact function so the launcher can invoke it without an import', () => {
    template.hasResourceProperties('AWS::Lambda::Function', { FunctionName: 'gnome-bot-interact' });
  });

  it('tells the functions where fix runs report back', () => {
    const fns = Object.values(template.findResources('AWS::Lambda::Function'))
      .filter((f: any) => f.Properties.Environment?.Variables?.CALLBACK_URL);
    expect(fns.length).toBe(5);
    expect(JSON.stringify(fns[0].Properties.Environment.Variables.CALLBACK_URL)).toContain('/api/github/callback');
  });

  it('queues alerts the registry posted to Slack for triage', () => {
    template.hasResourceProperties('AWS::Events::Rule', {
      EventBusName: 'gnome-alerts',
      EventPattern: { source: ['gnome.alerts.delivered'], 'detail-type': ['AlertDelivered'] },
    });
    template.hasResourceProperties('AWS::SQS::QueuePolicy', {
      PolicyDocument: { Statement: [Match.objectLike({ Principal: { Service: 'events.amazonaws.com' } })] },
    });
  });

  it('gives the worker a fixed role name the pipelines account can trust', () => {
    template.hasResourceProperties('AWS::IAM::Role', { RoleName: 'gnome-bot-worker' });
  });

  it('answers one question per worker invocation, with a dead-letter queue', () => {
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', { BatchSize: 1 });
    template.hasResourceProperties('AWS::SQS::Queue', {
      RedrivePolicy: Match.objectLike({ maxReceiveCount: 2 }),
      VisibilityTimeout: 360,
    });
  });

  it('lets only the ingress read the signing secret and the worker read the bot secrets', () => {
    const policies = Object.values(template.findResources('AWS::IAM::Policy')).map((p) => JSON.stringify(p));
    const ingress = policies.filter((p) => p.includes('slack-signing-secret'));
    expect(ingress).toHaveLength(1);
    expect(ingress[0]).not.toContain('gnome-bot/*');
    expect(policies.some((p) => p.includes('secret:gnome-bot/*') && p.includes('slack-bot-token'))).toBe(true);
  });

  it('writes nothing outside its own table and queue', () => {
    const actions = Object.values(template.findResources('AWS::IAM::Policy'))
      .flatMap((p: any) => p.Properties.PolicyDocument.Statement)
      .flatMap((s: any) => [s.Action].flat());
    const writes = actions.filter((a: string) => /:(Put|Delete|Update|Create|Start|Stop|Send|Invoke(?!Model))/.test(a));
    expect(new Set(writes)).toEqual(new Set([
      'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem', 'sqs:SendMessage', 'sqs:DeleteMessage',
    ]));
  });

  it('creates the worker log group itself, so the failure filter can attach on the first deploy', () => {
    template.hasResourceProperties('AWS::Logs::LogGroup', { RetentionInDays: 30 });
    template.hasResourceProperties('AWS::Lambda::Function', { LoggingConfig: { LogGroup: Match.anyValue() } });
  });

  it('builds alarms the alerting stack can read', () => {
    const names = Object.values(Template.fromStack(monitoring).findResources('AWS::CloudWatch::Alarm'))
      .map((a) => a.Properties.AlarmName).sort();
    expect(names).toEqual(['Bot--Answers-AnswerFailures-Warning', 'Bot--SlackEvents-Fault-Count-Warning',
      'Bot--WorkQueue-DLQ-Queue-Message-Count-Max-Warning', 'Bot--WorkQueue-Queue-Message-Age-Max-Warning',
      'Bot--Worker-Fault-Count-Warning', 'Bot--Interact-Fault-Count-Warning', 'Bot--GithubCallback-Fault-Count-Warning',
      'Bot--GithubWebhook-Fault-Count-Warning'].sort());
  });
});

describe('BotPipelineStack', () => {
  const stack = new BotPipelineStack(app(), 'Pipeline', { env: GnomeAccount.InfraPipelines.environment });
  const template = Template.fromStack(stack);

  it('trusts only the stages\' worker roles to read deploy history', () => {
    const role = Object.values(template.findResources('AWS::IAM::Role'))
      .find((r: any) => r.Properties.RoleName === 'gnome-bot-pipelines-read') as any;
    const statements = role.Properties.AssumeRolePolicyDocument.Statement;
    const accounts = statements.map((s: any) => JSON.stringify(s.Principal).match(/iam::(\d+):root/)![1]);
    expect(accounts).toEqual([GnomeAccount.InfraDev.accountId, GnomeAccount.InfraProd.accountId]);
    for (const statement of statements) {
      expect(statement.Condition.ArnEquals['aws:PrincipalArn']).toEqual([
        `arn:aws:iam::${GnomeAccount.InfraDev.accountId}:role/gnome-bot-worker`,
        `arn:aws:iam::${GnomeAccount.InfraProd.accountId}:role/gnome-bot-worker`,
      ]);
    }
  });
});
