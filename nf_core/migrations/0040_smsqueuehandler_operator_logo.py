# Generated by Django 4.0.7 on 2022-11-07 08:45

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('nf_core', '0039_smshistory_api_response'),
    ]

    operations = [
        migrations.AddField(
            model_name='smsqueuehandler',
            name='operator_logo',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]
