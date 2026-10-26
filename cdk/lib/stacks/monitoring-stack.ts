import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { DefaultDashboardFactory, MonitoringFacade, noopAction } from 'cdk-monitoring-constructs';

interface Props extends cdk.StackProps {
  ingressFn: lambda.IFunction;
  workerFn: lambda.IFunction;
  interactFn: lambda.IFunction;
  callbackFn: lambda.IFunction;
  webhookFn: lambda.IFunction;
  workerLogGroup: logs.ILogGroup;
  queue: sqs.IQueue;
  dlq: sqs.IQueue;
}

// The bot's alarms. They carry no action: the registry's alerting stack hears every alarm change through EventBridge
// and reads the severity from the tier each is declared under. Everything is a Warning: a broken bot is an
// inconvenience, never a reason to page.
export class BotMonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const monitoring = new MonitoringFacade(this, 'BotDashboard', {
      // Only the alarms are used; each dashboard is billed monthly and nobody reads them.
      dashboardFactory: new DefaultDashboardFactory(this, 'BotDashboardFactory', {
        dashboardNamePrefix: 'BotDashboard',
        createDashboard: false,
      }),
      alarmFactoryDefaults: {
        actionsEnabled: true,
        alarmNamePrefix: 'Bot-',
        action: noopAction(),
        datapointsToAlarm: 1,
      },
    });

    monitoring
      .monitorSqsQueueWithDlq({
        queue: props.queue,
        deadLetterQueue: props.dlq,
        humanReadableName: 'Work Queue',
        alarmFriendlyName: 'WorkQueue',
        addDeadLetterQueueMaxSizeAlarm: {
          Warning: { maxMessageCount: 0 },
        },
        addQueueMaxMessageAgeAlarm: {
          Warning: { maxAgeInSeconds: 10 * 60 },
        },
      })
      .monitorLambdaFunction({
        lambdaFunction: props.ingressFn,
        humanReadableName: 'Slack Events',
        alarmFriendlyName: 'SlackEvents',
        addFaultCountAlarm: {
          Warning: { maxErrorCount: 0 },
        },
      })
      .monitorLambdaFunction({
        lambdaFunction: props.workerFn,
        humanReadableName: 'Worker',
        alarmFriendlyName: 'Worker',
        addFaultCountAlarm: {
          Warning: { maxErrorCount: 0 },
        },
      })
      .monitorLambdaFunction({
        lambdaFunction: props.interactFn,
        humanReadableName: 'Interact',
        alarmFriendlyName: 'Interact',
        addFaultCountAlarm: {
          Warning: { maxErrorCount: 0 },
        },
      })
      .monitorLambdaFunction({
        lambdaFunction: props.callbackFn,
        humanReadableName: 'GitHub Callback',
        alarmFriendlyName: 'GithubCallback',
        addFaultCountAlarm: {
          Warning: { maxErrorCount: 0 },
        },
      })
      .monitorLambdaFunction({
        lambdaFunction: props.webhookFn,
        humanReadableName: 'GitHub Webhook',
        alarmFriendlyName: 'GithubWebhook',
        addFaultCountAlarm: {
          Warning: { maxErrorCount: 0 },
        },
      });

    // The worker answers its own failures in the thread instead of raising, so they only show in its logs.
    const answerFailures = new logs.MetricFilter(this, 'AnswerFailuresFilter', {
      logGroup: props.workerLogGroup,
      metricNamespace: 'GnomeBot',
      metricName: 'AnswerFailures',
      filterPattern: logs.FilterPattern.literal('"Couldn\'t answer"'),
      metricValue: '1',
    });
    monitoring.monitorCustom({
      humanReadableName: 'Answers',
      alarmFriendlyName: 'Answers',
      metricGroups: [{
        title: 'Questions the worker failed to answer',
        metrics: [{
          metric: answerFailures.metric({ period: cdk.Duration.minutes(5), statistic: 'Sum' }),
          alarmFriendlyName: 'AnswerFailures',
          addAlarm: {
            Warning: {
              threshold: 3,
              comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
              treatMissingDataOverride: cloudwatch.TreatMissingData.NOT_BREACHING,
              additionalDescription: 'The worker logged "Couldn\'t answer" 3+ times in 5 minutes',
            },
          },
        }],
      }],
    });
  }
}
