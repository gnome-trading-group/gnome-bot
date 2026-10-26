import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as pipelines from 'aws-cdk-lib/pipelines';
import * as secrets from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';
import { Stage } from '@gnome-trading-group/gnome-shared-cdk';
import { BotConfig, CONFIGS, GITHUB_BRANCH, GITHUB_REPO, PIPELINES_READ_ROLE_NAME, WORKER_ROLE_NAME } from './config';
import { BotStack } from './stacks/bot-stack';
import { BotMonitoringStack } from './stacks/monitoring-stack';

class AppStage extends cdk.Stage {
  constructor(scope: Construct, id: string, config: BotConfig) {
    super(scope, id, { env: config.account.environment });

    const botStack = new BotStack(this, 'BotStack', { stage: config.account.stage });
    new BotMonitoringStack(this, 'BotMonitoringStack', {
      ingressFn: botStack.ingressFn,
      workerFn: botStack.workerFn,
      workerLogGroup: botStack.workerLogGroup,
      interactFn: botStack.interactFn,
      callbackFn: botStack.callbackFn,
      queue: botStack.queue,
      dlq: botStack.dlq,
    });
  }
}

export class BotPipelineStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const npmSecret = secrets.Secret.fromSecretNameV2(this, 'NPMToken', 'npm-token');
    const dockerHubCredentials = secrets.Secret.fromSecretNameV2(this, 'DockerHub', 'docker-hub-credentials');

    // Lets each stage's bot read deploy history here. Trusted by account plus the worker role's ARN as a condition
    // rather than by the role itself: a trust policy naming a role that doesn't exist yet is refused, and this stack
    // deploys before the stages do.
    const stageAccounts = [CONFIGS[Stage.DEV]!, CONFIGS[Stage.PROD]!].map((c) => c.account.accountId);
    const readRole = new iam.Role(this, 'PipelinesReadRole', {
      roleName: PIPELINES_READ_ROLE_NAME,
      assumedBy: new iam.CompositePrincipal(...stageAccounts.map((account) => new iam.AccountPrincipal(account)))
        .withConditions({
          ArnEquals: { 'aws:PrincipalArn': stageAccounts.map((a) => `arn:aws:iam::${a}:role/${WORKER_ROLE_NAME}`) },
        }),
    });
    readRole.addToPolicy(new iam.PolicyStatement({
      actions: ['codepipeline:ListPipelines', 'codepipeline:GetPipelineState', 'codepipeline:ListPipelineExecutions'],
      resources: ['*'],
    }));

    const pipeline = new pipelines.CodePipeline(this, 'BotPipeline', {
      crossAccountKeys: true,
      pipelineName: 'BotPipeline',
      synth: new pipelines.ShellStep('deploy', {
        input: pipelines.CodePipelineSource.gitHub(GITHUB_REPO, GITHUB_BRANCH),
        commands: [
          'echo "//npm.pkg.github.com/:_authToken=${NPM_TOKEN}" > ~/.npmrc',
          'cd cdk/',
          'npm ci',
          'npx cdk synth',
        ],
        env: {
          NPM_TOKEN: npmSecret.secretValue.unsafeUnwrap(),
        },
        primaryOutputDirectory: 'cdk/cdk.out',
      }),
      dockerCredentials: [
        pipelines.DockerCredential.dockerHub(dockerHubCredentials),
      ],
    });

    pipeline.addStage(new AppStage(this, 'Dev', CONFIGS[Stage.DEV]!));
    pipeline.addStage(new AppStage(this, 'Prod', CONFIGS[Stage.PROD]!), {
      pre: [new pipelines.ManualApprovalStep('ApproveProd')],
    });

    pipeline.buildPipeline();
    npmSecret.grantRead(pipeline.synthProject.role!!);
    npmSecret.grantRead(pipeline.pipeline.role);
  }
}
