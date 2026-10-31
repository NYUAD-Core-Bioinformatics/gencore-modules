from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
import os
import re
import uuid

from flask_mail import Mail
from werkzeug.middleware.proxy_fix import ProxyFix
from wtforms import Form, StringField
from wtforms.validators import InputRequired, ValidationError

from catalog import (
    find_highest_installed_package,
    find_installed_package,
    load_packages,
)
from conda_registry import RegistryUnavailable, resolve_conda_package
from database import PackageRequest, Session, get_request, init_database

app = Flask(__name__)
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
    x_port=1,
)
init_database()

# Email configuration. Values are injected by Docker Compose from .env.
def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def required_env(name):
    """Return a required environment value or fail with a useful message."""
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


app.config['MAIL_SERVER'] = required_env('MAIL_SERVER')
app.config['MAIL_PORT'] = int(required_env('MAIL_PORT'))
app.config['MAIL_USE_TLS'] = env_flag('MAIL_USE_TLS', True)
app.config['MAIL_USE_SSL'] = env_flag('MAIL_USE_SSL', False)
app.config['MAIL_USERNAME'] = required_env('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = required_env('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = required_env('MAIL_DEFAULT_SENDER')
app.config['MAIL_SENDER_NAME'] = required_env('MAIL_SENDER_NAME')
app.config['SUPPORT_EMAIL'] = required_env('SUPPORT_EMAIL')

if app.config['MAIL_USE_TLS'] and app.config['MAIL_USE_SSL']:
    raise RuntimeError('MAIL_USE_TLS and MAIL_USE_SSL cannot both be true')

#Define mail app function.
mail = Mail(app)


#Email validation 
def validate_emails(form, field):
    emails = field.data
    email_id = field.data.split(',')
    regex = r'^\S+@\S+\.\S+$'
    invalid_emails = []
    for email in email_id:
        if not re.match(regex, email.strip()):
            invalid_emails.append(email.strip())
    if invalid_emails:
        raise ValidationError('Invalid email address format: {}'.format(', '.join(invalid_emails)))

#Default route for index
@app.route('/')
def index():
    packages = load_packages()
    return render_template(
        'index.html',
        package_count=len(packages),
        software_count=len({pkg['name'].lower() for pkg in packages}),
        generation_count=len({pkg['master_version'] for pkg in packages}),
    )


@app.get('/healthz')
def healthcheck():
    """Container health endpoint; does not depend on SMTP or the HPC."""
    return {'status': 'ok'}, 200

#Email form submission

class HowtoForm(Form):
    name = StringField('Full name', [InputRequired()], render_kw={"placeholder": "Your full name"})
    email = StringField('Email address',  [InputRequired(),validate_emails], render_kw={"placeholder": "name@nyu.edu"})
    soft_name = StringField('Software name', [InputRequired()], render_kw={"placeholder": "For example: samtools"})
    soft_version = StringField('Software version', render_kw={"placeholder": "Type latest or specify a version"})
    conda_url = StringField('Conda package URL')
    github_url = StringField('GitHub project URL')
    database_url = StringField('Database download URL')
    


@app.route('/howto', methods=['GET', 'POST'])
def howto():

    form=HowtoForm(request.form)
    if  request.method == 'POST' and form.validate():
        requested_version = (form.soft_version.data or '').strip()
        if not requested_version or requested_version.casefold() == 'latest':
            requested_version = 'latest'

        data = {
            'request_id': str(uuid.uuid4()),
            'name': form.name.data,
            'email': form.email.data,
            'soft_name': form.soft_name.data.strip(),
            'soft_version': requested_version,
            'conda_url': (form.conda_url.data or '').strip(),
            'github_url': (form.github_url.data or '').strip(),
            'database_url': (form.database_url.data or '').strip(),
        }

        try:
            if data['soft_version'].casefold() != 'latest':
                installed_package = find_installed_package(
                    data['soft_name'],
                    data['soft_version'],
                )
            else:
                local_latest = find_highest_installed_package(data['soft_name'])
                installed_package = None
                if local_latest:
                    try:
                        upstream_latest = resolve_conda_package(
                            data['soft_name'],
                            'latest',
                            data['conda_url'],
                            timeout=10,
                        )
                    except RegistryUnavailable:
                        app.logger.warning(
                            "Anaconda API unavailable during immediate latest-version check"
                        )
                    else:
                        if upstream_latest is None:
                            # Manually installed software may not exist in an
                            # approved channel but is still available locally.
                            installed_package = local_latest
                        else:
                            installed_package = find_installed_package(
                                data['soft_name'],
                                upstream_latest.version,
                            )
        except Exception:
            app.logger.exception(
                "Unable to check the local catalog before queuing the request"
            )
            installed_package = None

        if installed_package:
            return redirect(
                url_for(
                    'package_detail',
                    package_name=installed_package['name'],
                    package_version=installed_package['version'],
                    master_version=installed_package['master_version'],
                    already_available='1',
                )
            )

        try:
            with Session.begin() as session:
                session.add(
                    PackageRequest(
                        request_id=data['request_id'],
                        requester_name=data['name'],
                        requester_email=data['email'],
                        package_name=data['soft_name'],
                        requested_version=data['soft_version'],
                        conda_url=data['conda_url'] or None,
                        github_url=data['github_url'] or None,
                        database_url=data['database_url'] or None,
                        status='RECEIVED',
                    )
                )

            from tasks import process_package_request

            process_package_request.delay(data['request_id'])
        except Exception as e:
            app.logger.exception("Unable to record or queue package request")
            return render_template(
                'howto_response.html',
                form=form,
                message="The request could not be queued. Please try again or contact the administrator.",
            ), 503

        return render_template(
            'howto_response.html',
            form=form,
            request_id=data['request_id'],
            message="Form submitted successfully. We are checking whether the requested module is already available.",
        )
    return render_template("howto.html", form = form)


@app.get('/requests/<uuid:request_id>')
def request_status(request_id):
    package_request = get_request(str(request_id))
    if package_request is None:
        abort(404)
    redirect_url = None
    if (
        package_request.status == 'ALREADY_AVAILABLE'
        and package_request.resolved_version
    ):
        installed_package = find_installed_package(
            package_request.package_name,
            package_request.resolved_version,
        )
        if installed_package:
            redirect_url = url_for(
                'package_detail',
                package_name=installed_package['name'],
                package_version=installed_package['version'],
                master_version=installed_package['master_version'],
                already_available='1',
            )
    return jsonify(
        {
            'request_id': package_request.request_id,
            'package': package_request.package_name,
            'requested_version': package_request.requested_version,
            'resolved_version': package_request.resolved_version,
            'channel': package_request.channel,
            'status': package_request.status,
            'detail': package_request.status_detail,
            'slurm_job_id': package_request.slurm_job_id,
            'redirect_url': redirect_url,
            'updated_at': package_request.updated_at.isoformat(),
        }
    )


@app.route('/package/<package_name>/<package_version>/<master_version>')
def package_detail(package_name, package_version, master_version):
    packages = load_packages()
    package = None
    for pkg in packages:
        if pkg['name'] == package_name and  pkg['version'] == package_version and pkg['master_version'] == master_version:
            package = pkg
            break
    if package is None:
        return render_template('404.html'), 404
    return render_template('package_detail.html', package=package)



@app.route('/browse', methods=['GET', 'POST'])
@app.route('/browse/<letter>', methods=['GET', 'POST'])
def browse_packages(letter=None):
    packages = load_packages()
    search_query = request.values.get('search', '').strip()
    if search_query:
        filtered_packages = [pkg for pkg in packages if search_query.lower() in pkg['name'].lower()]
    elif letter:
        filtered_packages = []
        for pkg in packages:
            if pkg['name'].lower().startswith(letter.lower()):
                filtered_packages.append(pkg)
    else:
        filtered_packages = packages
    filtered_packages = sorted(filtered_packages, key=lambda pkg: pkg['name'].lower())
    return render_template(
        'browse_packages.html',
        packages=filtered_packages,
        search_query=search_query,
        active_letter=letter,
        total_packages=len(packages),
    )

if __name__ == "__main__":
    app.run(debug=True)
#app.run(ssl_context=("gencore-modules_abudhabi_nyu_edu_cert.cer", "gencore-modules-key"))
