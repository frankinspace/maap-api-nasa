from datetime import timedelta, datetime

import flask
import requests
from flask import abort, request, json
from xmltodict import parse
from flask import current_app
from .cas_urls import create_cas_proxy_url
from .cas_urls import create_cas_proxy_validate_url
from api.maap_database import db
from api.models.member import Member
from api.models.member_session import MemberSession
from api.models.member_job import MemberJob
from api import settings
from functools import wraps
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from Crypto import Random
from Crypto.Hash import SHA
from base64 import b64decode
import ast


try:
    from urllib import urlopen
except ImportError:
    from urllib.request import urlopen

blueprint = flask.Blueprint('cas', __name__)

PROXY_TICKET_PREFIX = "PGT-"


def validate_proxy(ticket):
    """
    Will attempt to validate the proxy ticket. If validation fails, then None
    is returned. If validation is successful, then a Member object is returned
    and the validated proxy ticket is saved in the session db table while the
    validated attributes are saved under member db table.
    """

    current_app.logger.debug("validating token {0}".format(ticket))

    decrypted_ticket = decrypt_proxy_ticket(ticket)

    cas_session = db.session.query(MemberSession).filter_by(session_key=decrypted_ticket).first()

    # Check for session created timestamp < 24 hours old
    if cas_session is not None and cas_session.creation_date + timedelta(hours=24) > datetime.utcnow():
        return cas_session
    else:
        cas_validate_proxy_url = create_cas_proxy_url(
            current_app.config['CAS_SERVER'],
            request.base_url,
            decrypted_ticket
        )

        cas_response = validate_cas_request(cas_validate_proxy_url)

        if cas_response[0]:
            current_app.logger.debug("valid proxy granting ticket")

            xml_from_dict = cas_response[1]["cas:serviceResponse"]["cas:proxySuccess"]
            proxy_ticket = xml_from_dict["cas:proxyTicket"]

            proxy_validate_url = create_cas_proxy_validate_url(
                current_app.config['CAS_SERVER'],
                request.base_url,
                proxy_ticket
            )

            cas_proxy_response = validate_cas_request(proxy_validate_url)

            if cas_proxy_response[0]:
                return start_member_session(cas_proxy_response, decrypted_ticket)

    current_app.logger.debug("invalid proxy granting ticket")
    return None


def validate_dps_job(dps_token, job_id):
    """
    Will attempt to validate a dps token and job id. If validation fails, then None
    is returned. If validation is successful, then a Member object is returned.
    """
    current_app.logger.debug("validating dps token / job id: {0} / {1}".format(dps_token, job_id))

    if dps_token != settings.DPS_MACHINE_TOKEN:
        return None

    dps_job = db.session.query(MemberJob).filter_by(job_id=job_id).first()

    if dps_job is None:
        return None

    return dps_job.member


def validate_bearer(token):
    """
    Will attempt to validate the bearer token. If validation fails, then None
    is returned. If validation is successful, then a Member object is returned.
    """

    current_app.logger.debug("validating token {0}".format(token))

    resp = requests.get(current_app.config['CAS_SERVER'] + '/oauth2.0/profile',
                        headers={'Authorization': 'Bearer ' + token})

    if resp.status_code == 200:
        return json.loads(resp.text)

    current_app.logger.debug("invalid bearer token")
    return None


def validate_cas_request(cas_url):

    xml_from_dict = {}
    is_valid = False

    current_app.logger.debug("Making GET request to {0}".format(
        cas_url))

    try:
        xmldump = urlopen(cas_url).read().strip().decode('utf8', 'ignore')
        xml_from_dict = parse(xmldump)
        is_valid = True if "cas:authenticationSuccess" in xml_from_dict["cas:serviceResponse"] or \
                           "cas:proxySuccess" in xml_from_dict["cas:serviceResponse"] else False
    except ValueError:
        current_app.logger.error("CAS returned unexpected result")

    return is_valid, xml_from_dict


def start_member_session(cas_response, ticket):

    xml_from_dict = cas_response[1]["cas:serviceResponse"]["cas:authenticationSuccess"]
    attributes = xml_from_dict.get("cas:attributes", {})
    usr = get_cas_attribute_value(attributes, 'preferred_username')

    member = db.session.query(Member).filter_by(username=usr).first()
    urs_access_token = get_cas_attribute_value(attributes, 'access_token')

    if member is None:
        member = Member(first_name=get_cas_attribute_value(attributes, 'given_name'),
                        last_name=get_cas_attribute_value(attributes, 'family_name'),
                        username=usr,
                        email=get_cas_attribute_value(attributes, 'email'),
                        organization=get_cas_attribute_value(attributes, 'organization'),
                        urs_token=urs_access_token)
        db.session.add(member)
    else:
        member.urs_token = urs_access_token

    db.session.commit()

    member_session = MemberSession(member_id=member.id, session_key=ticket, creation_date=datetime.utcnow())
    db.session.add(member_session)
    db.session.commit()

    return member_session


def get_cas_attribute_value(attributes, attribute_key):

    if attributes and "cas:" + attribute_key in attributes:
        return attributes["cas:" + attribute_key]
    else:
        return ''


def decrypt_proxy_ticket(ticket):
    if ticket.startswith(PROXY_TICKET_PREFIX):
        return ticket
    else:
        try:
            key = RSA.import_key(settings.CAS_PROXY_DECRYPTION_TOKEN)
            dsize = SHA.digest_size
            sentinel = Random.new().read(15 + dsize)
            decryptor = PKCS1_v1_5.new(key)
            decrypted = decryptor.decrypt(ast.literal_eval(str(b64decode(ticket))), sentinel)

            return decrypted.decode("utf-8")
        except:
            current_app.logger.debug("invalid proxy granting ticket")
            return ''


def get_authorized_user():
    if 'proxy-ticket' in request.headers:
        member_session = validate_proxy(request.headers['proxy-ticket'])

        if member_session is not None:
            return member_session.member

    if 'Authorization' in request.headers:
        bearer = request.headers.get('Authorization')
        token = bearer.split()[1]
        authorized = validate_bearer(token)

        if authorized is not None:
            return authorized

    return None


def get_dps_user():
    if _is_valid_dps_request():
        dps_user = validate_dps_job(request.headers['dps-machine-token'], request.headers['dps-job-id'])
        return dps_user

    return None


def login_required(wrapped_function):
    @wraps(wrapped_function)
    def wrap(*args, **kwargs):

        if 'proxy-ticket' in request.headers:
            authorized = validate_proxy(request.headers['proxy-ticket'])

            if authorized is not None:
                return wrapped_function(*args, **kwargs)

        if 'Authorization' in request.headers:
            bearer = request.headers.get('Authorization')
            token = bearer.split()[1]
            authorized = validate_bearer(token)

            if authorized is not None:
                return wrapped_function(*args, **kwargs)

        abort(403, description="Not authorized.")


def dps_authorized(wrapped_function):
    @wraps(wrapped_function)
    def wrap(*args, **kwargs):

        if _is_valid_dps_request():
            authorized = validate_dps_job(request.headers['dps-machine-token'], request.headers['dps-job-id'])

            if authorized is not None:
                return wrapped_function(*args, **kwargs)

        abort(403, description="Not authorized.")


def _is_valid_dps_request():
    return 'dps-machine-token' in request.headers and 'dps-job-id' in request.headers

