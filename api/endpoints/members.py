import logging
from flask_restplus import Resource, reqparse
from flask import request, jsonify, Response, make_response
from api.restplus import api
import api.settings as settings
from api.cas.cas_auth import get_authorized_user, login_required
from api.maap_database import db
from api.models.member import Member, MemberSchema
from datetime import datetime
import json
import boto3
import requests
from urllib import parse


log = logging.getLogger(__name__)
ns = api.namespace('members', description='Operations for MAAP members')
s3_client = boto3.client('s3', region_name=settings.AWS_REGION)


@ns.route('/self')
class Self(Resource):

    @login_required
    def get(self):
        member = get_authorized_user()

        if 'proxy-ticket' in request.headers:
            member_schema = MemberSchema()
            return json.loads(member_schema.dumps(member))
        if 'Authorization' in request.headers:
            return member


@ns.route('/selfTest')
class SelfTest(Resource):

    @login_required
    def get(self):
        member = get_authorized_user()
        member_schema = MemberSchema()

        return json.loads(member_schema.dumps(member))


@ns.route('/self/sshKey')
class PublicSshKeyUpload(Resource):

    @login_required
    def post(self):
        if 'file' not in request.files:
            log.error('Upload attempt with no file')
            raise Exception('No file uploaded')

        member = get_authorized_user()

        f = request.files['file']

        file_lines = f.read().decode("utf-8")

        db.session.query(Member).filter(Member.id == member.id). \
            update({Member.public_ssh_key: file_lines,
                    Member.public_ssh_key_name: f.filename,
                    Member.public_ssh_key_modified_date: datetime.utcnow()})

        db.session.commit()

        member_schema = MemberSchema()
        return json.loads(member_schema.dumps(member))

    @login_required
    def delete(self):
        member = get_authorized_user()

        db.session.query(Member).filter(Member.id == member.id). \
            update({Member.public_ssh_key: '',
                    Member.public_ssh_key_name: '',
                    Member.public_ssh_key_modified_date: datetime.utcnow()})

        db.session.commit()

        member_schema = MemberSchema()
        return json.loads(member_schema.dumps(member))


@ns.route('/self/presignedUrlS3/<string:bucket>/<path:key>')
class PresignedUrlS3(Resource):

    expiration_param = reqparse.RequestParser()
    expiration_param.add_argument('exp', type=int, required=False, default=60 * 60 * 12)
    expiration_param.add_argument('ws', type=str, required=False, default="")

    @login_required
    @api.expect(expiration_param)
    def get(self, bucket, key):

        expiration = request.args['exp']
        che_ws_namespace = request.args['ws'] if 'ws' in request.args else ''
        s3_path = self.mount_key_to_bucket(key, che_ws_namespace) if che_ws_namespace else key

        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': 'maap-ops-workspace', #bucket, (put this back to {bucket} once environments are finalized!)
                'Key': parse.unquote(s3_path)
            },
            ExpiresIn=expiration

        )

        response = jsonify(url=url)
        response.headers.add('Access-Control-Allow-Origin', '*')

        return response

    def mount_key_to_bucket(self, key, ws):

        if key.startswith(settings.WORKSPACE_MOUNT_PRIVATE):
            return key.replace(settings.WORKSPACE_MOUNT_PRIVATE, ws)
        elif key.startswith(settings.WORKSPACE_MOUNT_PUBLIC):
            return key.replace(settings.WORKSPACE_MOUNT_PUBLIC, f'{settings.AWS_SHARED_WORKSPACE_BUCKET_PATH}/{ws}')
        elif key.startswith(settings.WORKSPACE_MOUNT_SHARED):
            return key.replace(settings.WORKSPACE_MOUNT_SHARED, settings.AWS_SHARED_WORKSPACE_BUCKET_PATH)
        else:
            return key


@ns.route('/self/awsAccess/requesterPaysBucket')
class AwsAccessRequesterPaysBucket(Resource):

    expiration_param = reqparse.RequestParser()
    expiration_param.add_argument('exp', type=int, required=False, default=60 * 60 * 12)

    @login_required
    @api.expect(expiration_param)
    def get(self):

        member = get_authorized_user()

        expiration = request.args['exp']
        sts_client = boto3.client('sts')
        assumed_role_object = sts_client.assume_role(
            DurationSeconds=int(expiration),
            RoleArn=settings.AWS_REQUESTER_PAYS_BUCKET_ARN,
            RoleSessionName="MAAP-session-" + member.username
        )
        credentials = assumed_role_object['Credentials']

        response = jsonify(
            aws_access_key_id=credentials['AccessKeyId'],
            aws_secret_access_key=credentials['SecretAccessKey'],
            aws_session_token=credentials['SessionToken']
        )

        response.headers.add('Access-Control-Allow-Origin', '*')

        return response


@ns.route('/self/awsAccess/edcCredentials/<string:endpoint_uri>')
class AwsAccessEdcCredentials(Resource):
    """
    Earthdata Cloud Temporary s3 Credentials

        Obtain temporary s3 credentials to access Earthdata Cloud resources

        Example:
        https://api.maap-project.org/api/self/edcCredentials/https%3A%2F%2Fdata.lpdaac.earthdatacloud.nasa.gov%2Fs3credentials
    """
    @login_required
    def get(self, endpoint_uri):

        s = requests.Session()
        maap_user = get_authorized_user()

        if maap_user is None:
            return Response('Unauthorized', status=401)
        else:
            urs_token = db.session.query(Member).filter_by(id=maap_user.id).first().urs_token
            s.headers.update({'Authorization': f'Bearer {urs_token},Basic {settings.MAAP_EDL_CREDS}',
                              'Connection': 'close'})

            endpoint = parse.unquote(endpoint_uri)
            login_resp = s.get(
                endpoint, allow_redirects=False
            )
            login_resp.raise_for_status()

            edl_response = s.get(url=login_resp.headers['location'])
            json_response = json.loads(edl_response.content)

            response = jsonify(
                accessKeyId=json_response['accessKeyId'],
                secretAccessKey=json_response['secretAccessKey'],
                sessionToken=json_response['sessionToken'],
                expiration=json_response['expiration']
            )

            response.headers.add('Access-Control-Allow-Origin', '*')

            return response






