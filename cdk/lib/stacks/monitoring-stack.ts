import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as sqs from 'aws-cdk-lib/aws-sqs';

interface Props extends cdk.StackProps {
  ingressFn: lambda.IFunction;
  workerFn: lambda.IFunction;
  queue: sqs.IQueue;
  dlq: sqs.IQueue;
}

// The bot's alarms. They carry no action: the registry's alerting stack hears every alarm change through EventBridge
// and reads the severity from the end of the name. Nothing here pages; a broken bot is an inconvenience.
export class BotMonitoringStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: Props) {
    super(scope, id, props);

    const alarm = (name: string, tier: 'Error' | 'Warning', metric: cloudwatch.Metric, threshold: number) =>
      metric.createAlarm(this, name, {
        alarmName: `Bot-${name}-${tier}`,
        threshold,
        evaluationPeriods: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    const fiveMinutes = cdk.Duration.minutes(5);

    alarm('Dlq', 'Warning', props.dlq.metricApproximateNumberOfMessagesVisible({ period: fiveMinutes }), 1);
    alarm('QueueAge', 'Warning',
      props.queue.metricApproximateAgeOfOldestMessage({ period: fiveMinutes, statistic: 'Maximum' }), 10 * 60);
    alarm('IngressErrors', 'Warning', props.ingressFn.metricErrors({ period: fiveMinutes, statistic: 'Sum' }), 1);
    alarm('WorkerErrors', 'Warning', props.workerFn.metricErrors({ period: fiveMinutes, statistic: 'Sum' }), 1);

    // The worker answers its own failures in the thread instead of raising, so they only show in its logs.
    const answerFailures = new logs.MetricFilter(this, 'AnswerFailuresFilter', {
      logGroup: logs.LogGroup.fromLogGroupName(this, 'WorkerLogs', `/aws/lambda/${props.workerFn.functionName}`),
      metricNamespace: 'GnomeBot',
      metricName: 'AnswerFailures',
      filterPattern: logs.FilterPattern.literal('"Couldn\'t answer"'),
      metricValue: '1',
    });
    alarm('AnswerFailures', 'Warning', answerFailures.metric({ period: fiveMinutes, statistic: 'Sum' }), 3);
  }
}
