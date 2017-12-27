import csv
import datetime
import inspect
import json
import logging
import re
import requests
import smtplib
import socket
import spf

import DNS
import dns.resolver
import dns.reversename

from trustymail.domain import Domain

# A cache for SMTP scanning results
_SMTP_CACHE = {}

MAILTO_REGEX = re.compile(r"mailto:([\w\-!#$%&'*+-/=?^_`{|}~][\w\-.!#$%&'*+-/=?^_`{|}~]+@[\w\-.]+)")


def domain_list_from_url(url):
    if not url:
        return []

    with requests.Session() as session:
        # Download current list of agencies, then let csv reader handle it.
        return domain_list_from_csv(session.get(url).content.decode('utf-8').splitlines())


def domain_list_from_csv(csv_file):
    domain_list = list(csv.reader(csv_file, delimiter=','))

    # Check the headers for the word domain - use that column.

    domain_column = 0

    for i in range(0, len(domain_list[0])):
        header = domain_list[0][i]
        if 'domain' in header.lower():
            domain_column = i
            # CSV starts with headers, remove first row.
            domain_list.pop(0)
            break

    domains = []
    for row in domain_list:
        domains.append(row[domain_column])

    return domains


def mx_scan(resolver, domain):
    try:
        # Use TCP, since we care about the content and correctness of the
        # records more than whether their records fit in a single UDP packet.
        for record in resolver.query(domain.domain_name, 'MX', tcp=True):
            domain.add_mx_record(record)
    except (dns.resolver.NoNameservers, dns.resolver.NoAnswer, dns.exception.Timeout, dns.resolver.NXDOMAIN) as error:
        handle_error('[MX]', domain, error)


def starttls_scan(domain, smtp_timeout, smtp_localhost, smtp_ports, smtp_cache):
    """Scan a domain to see if it supports SMTP and supports STARTTLS.

    Scan a domain to see if it supports SMTP.  If the domain does support
    SMTP, a further check will be done to see if it supports STARTTLS.
    All results are stored inside the Domain object that is passed in
    as a parameter.

    Parameters
    ----------
    domain : Domain
        The Domain to be tested.

    smtp_timeout : int
        The SMTP connection timeout in seconds.

    smtp_localhost : str
        The hostname to use when connecting to SMTP servers.

    smtp_ports : obj:`list` of :obj:`str`
        A comma-delimited list of ports at which to look for SMTP servers.

    smtp_cache : bool
        Whether or not to cache SMTP results.
    """
    for mail_server in domain.mail_servers:
        for port in smtp_ports:
            domain.ports_tested.add(port)
            server_and_port = mail_server + ':' + str(port)

            if not smtp_cache or (server_and_port not in _SMTP_CACHE):
                domain.starttls_results[server_and_port] = {}

                smtp_connection = smtplib.SMTP(timeout=smtp_timeout,
                                               local_hostname=smtp_localhost)
                logging.debug('Testing ' + server_and_port + ' for STARTTLS support')
                # Try to connect.  This will tell us if something is
                # listening.
                try:
                    smtp_connection.connect(mail_server, port)
                    domain.starttls_results[server_and_port]['is_listening'] = True
                except (socket.timeout, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, ConnectionRefusedError, OSError) as error:
                    handle_error('[STARTTLS]', domain, error)
                    domain.starttls_results[server_and_port]['is_listening'] = False
                    domain.starttls_results[server_and_port]['supports_smtp'] = False
                    domain.starttls_results[server_and_port]['starttls'] = False

                    if smtp_cache:
                        _SMTP_CACHE[server_and_port] = domain.starttls_results[server_and_port]

                    continue

                # Now try to say hello.  This will tell us if the
                # thing that is listening is an SMTP server.
                try:
                    smtp_connection.ehlo_or_helo_if_needed()
                    domain.starttls_results[server_and_port]['supports_smtp'] = True
                    logging.debug('\t Supports SMTP')
                except (smtplib.SMTPHeloError, smtplib.SMTPServerDisconnected) as error:
                    handle_error('[STARTTLS]', domain, error)
                    domain.starttls_results[server_and_port]['supports_smtp'] = False
                    domain.starttls_results[server_and_port]['starttls'] = False
                    # smtplib freaks out if you call quit on a non-open
                    # connection
                    try:
                        smtp_connection.quit()
                    except smtplib.SMTPServerDisconnected as error2:
                        handle_error('[STARTTLS]', domain, error2)

                    if smtp_cache:
                        _SMTP_CACHE[server_and_port] = domain.starttls_results[server_and_port]

                    continue

                # Now check if the server supports STARTTLS.
                has_starttls = smtp_connection.has_extn('STARTTLS')
                domain.starttls_results[server_and_port]['starttls'] = has_starttls
                logging.debug('\t Supports STARTTLS: ' + str(has_starttls))

                # Close the connection
                # smtplib freaks out if you call quit on a non-open
                # connection
                try:
                    smtp_connection.quit()
                except smtplib.SMTPServerDisconnected as error:
                    handle_error('[STARTTLS]', domain, error)

                # Copy the results into the cache, if necessary
                if smtp_cache:
                    _SMTP_CACHE[server_and_port] = domain.starttls_results[server_and_port]
            else:
                logging.debug('\tUsing cached results for ' + server_and_port)
                # Copy the cached results into the domain object
                domain.starttls_results[server_and_port] = _SMTP_CACHE[server_and_port]


def check_spf_record(record_text, expected_result, domain):
    """Test to see if an SPF record is valid and correct.

    The record is tested by checking the response when we query if it
    allows us to send mail from an IP that is known not to be a mail
    server that appears in the MX records for ANY domain.

    Parameters
    ----------
    record_text : str
        The text of the SPF record to be tested.

    expected_result : str
        The expected result of the test.

    domain : trustymail.Domain
        The Domain object corresponding to the SPF record being
        tested.  Any errors will be logged to this object.
    """
    try:
        # Here I am using the IP address for c1b1.ncats.cyber.dhs.gov
        # (64.69.57.18) since it (1) has a valid PTR record and (2) is not
        # listed by anyone as a valid mail server.
        #
        # I'm actually temporarily using an IP that virginia.edu resolves to
        # until we resolve why Google DNS does not return the same PTR records
        # as the CAL DNS does for 64.69.57.18.
        query = spf.query('128.143.22.36', 'email_wizard@' + domain.domain_name, domain.domain_name, strict=2)
        response = query.check()

        response_type = response[0]
        if response_type == 'temperror' or response_type == 'permerror' or response_type == 'ambiguous':
            handle_error('[SPF]', domain, 'SPF query returned {}: {}'.format(response_type, response[2]))
        elif response_type == expected_result:
            # Everything checks out.  The SPF syntax seems valid
            domain.valid_spf = True
        else:
            domain.valid_spf = False
            msg = 'Result unexpectedly differs: Expected [{}] - actual [{}]'.format(expected_result, response_type)
            handle_error('[SPF]', domain, msg)
    except spf.AmbiguityWarning as error:
        handle_syntax_error('[SPF]', domain, error)


def get_spf_record_text(resolver, domain_name, domain, follow_redirect=False):
    """Get the SPF record text for the given domain name.

    DNS queries are performed using the dns.resolver.Resolver object.
    Errors are logged to the trustymail.Domain object.  The Boolean
    parameter indicates whether to follow redirects in SPF records.

    Parameters
    ----------
    resolver : dns.resolver.Resolver
        The Resolver object to use for DNS queries.

    domain_name : str
        The domain name to query for an SPF record.

    domain : trustymail.Domain
        The Domain object whose corresponding SPF record text is
        desired.  Any errors will be logged to this object.

    follow_redirect : bool
       A Boolean value indicating whether to follow redirects in SPF
       records.

    Returns
    -------
    str: The desired SPF record text
    """
    record_to_return = None
    try:
        # Use TCP, since we care about the content and correctness of the
        # records more than whether their records fit in a single UDP packet.
        for record in resolver.query(domain_name, 'TXT', tcp=True):
            record_text = record.to_text().strip('"')

            if not record_text.startswith('v=spf1'):
                # Not an spf record, ignore it.
                continue

            match = re.search('v=spf1\s*redirect=(\S*)', record_text)
            if follow_redirect and match:
                redirect_domain_name = match.group(1)
                record_to_return = get_spf_record_text(resolver, redirect_domain_name, domain)
            else:
                record_to_return = record_text
    except (dns.resolver.NoNameservers, dns.resolver.NoAnswer, dns.exception.Timeout, dns.resolver.NXDOMAIN) as error:
        handle_error('[SPF]', domain, error)

    return record_to_return


def spf_scan(resolver, domain):
    """Scan a domain to see if it supports SPF.  If the domain has an SPF
    record, verify that it properly rejects mail sent from an IP known
    to be disallowed.

    Parameters
    ----------
    resolver : dns.resolver.Resolver
        The Resolver object to use for DNS queries.

    domain : trustymail.Domain
        The Domain object being scanned for SPF support.  Any errors
        will be logged to this object.
    """
    # If an SPF record exists, record the raw SPF record text in the
    # Domain object
    record_text_not_following_redirect = get_spf_record_text(resolver, domain.domain_name, domain)
    if record_text_not_following_redirect:
        domain.spf.append(record_text_not_following_redirect)

    record_text_following_redirect = get_spf_record_text(resolver, domain.domain_name, domain, True)
    if record_text_following_redirect:
        # From the found record grab the specific result when something
        # doesn't match.  Definitions of result come from
        # https://www.ietf.org/rfc/rfc4408.txt
        if record_text_following_redirect.endswith('-all'):
            result = 'fail'
        elif record_text_following_redirect.endswith('?all'):
            result = 'neutral'
        elif record_text_following_redirect.endswith('~all'):
            result = 'softfail'
        elif record_text_following_redirect.endswith('all') or record_text_following_redirect.endswith('+all'):
            result = 'pass'
        else:
            result = 'neutral'

        check_spf_record(record_text_not_following_redirect, result, domain)


def dmarc_scan(resolver, domain):
    # dmarc records are kept in TXT records for _dmarc.domain_name.
    try:
        dmarc_domain = '_dmarc.%s' % domain.domain_name
        # Use TCP, since we care about the content and correctness of the
        # records more than whether their records fit in a single UDP packet.
        for record in resolver.query(dmarc_domain, 'TXT', tcp=True):
            record_text = record.to_text().strip('"')

            # Ensure the record is a DMARC record. Some domains that
            # redirect will cause an SPF record to show.
            if record_text.startswith('v=DMARC1'):
                domain.dmarc.append(record_text)

            # Remove excess whitespace
            record_text = record_text.strip()

            # DMARC records follow a specific outline as to how they are defined - tag:value
            # We can split this up into a easily manipulatable
            tag_dict = {}
            for options in record_text.split(';'):
                if '=' not in options:
                    continue
                tag = options.split('=')[0].strip()
                value = options.split('=')[1].strip()
                tag_dict[tag] = value

            if 'p' not in tag_dict:
                tag_dict['p'] = 'none'
            if 'sp' not in tag_dict:
                tag_dict['sp'] = tag_dict['p']
            if 'ri' not in tag_dict:
                tag_dict['ri'] = 86400
            if 'pct' not in tag_dict:
                tag_dict['pct'] = 100
            if 'adkim' not in tag_dict:
                tag_dict['adkim'] = 'r'
            if 'aspf'not in tag_dict:
                tag_dict['aspf'] = 'r'
            if 'ro' not in tag_dict:
                tag_dict['ro'] = '0'
            if 'rf' not in tag_dict:
                tag_dict['rf'] = 'afrf'
            if 'rua' not in tag_dict:
                domain.dmarc_has_aggregate_uri = True
            if 'ruf' not in tag_dict:
                domain.dmarc_has_forensic_uri = True

            for tag in tag_dict:
                if tag not in ['v', 'mailto', 'rf', 'p', 'sp', 'adkim', 'aspf', 'fo', 'pct', 'ri', 'rua', 'ruf']:
                    msg = 'Unknown DMARC tag {0}'.format(tag)
                    handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                    domain.valid_dmarc = False
                elif tag == 'p':
                    if tag_dict[tag] not in ['none', 'quarantine', 'reject']:
                        msg = 'Unknown DMARC policy {0}'.format(tag)
                        handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                        domain.valid_dmarc = False
                    else:
                        domain.dmarc_policy = tag_dict[tag]
                elif tag == 'sp':
                    if tag_dict[tag] not in ['none', 'quarantine', 'reject']:
                        msg = 'Unknown DMARC subdomain policy {0}'.format(tag_dict[tag])
                        handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                        domain.valid_dmarc = False
                elif tag == 'fo':
                    values = tag_dict[tag].split(':')
                    if '0' in values and '1' in values:
                        msg = 'fo tag values 0 and 1 are mutually exclusive'
                        handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                    for value in values:
                        if value not in ['0', '1', 'd', 's']:
                            msg = 'Unknown DMARC fo tag value {0}'.format(value)
                            handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                            domain.valid_dmarc = False
                elif tag == 'rf':
                    values = tag_dict[tag].split(':')
                    for value in values:
                        if value not in ['afrf']:
                            msg = 'Unknown DMARC rf tag value {0}'.format(value)
                            handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                            domain.valid_dmarc = False
                elif tag == 'ri':
                    try:
                        int(tag_dict[tag])
                    except ValueError:
                        msg = 'Invalid DMARC ri tag value: {0} - must be an integer'.format(tag_dict[tag])
                        handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                        domain.valid_dmarc = False
                elif tag == 'pct':
                    try:
                        pct = int(tag_dict[tag])
                        if pct < 0 or pct > 100:
                            msg = 'Error: invalid DMARC pct tag value: {0} - must be an integer between ' \
                                  '0 and 100'.format(tag_dict[tag])
                            handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                            domain.valid_dmarc = False
                        domain.dmarc_pct = pct
                    except ValueError:
                        msg = 'invalid DMARC pct tag value: {0} - must be an integer'.format(tag_dict[tag])
                        handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                        domain.valid_dmarc = False
                elif tag == 'rua' or tag == 'ruf':
                    uris = tag_dict[tag].split(',')
                    for uri in uris:
                        # mailto: is currently the only type of DMARC URI
                        mailto_matches = MAILTO_REGEX.findall(uri)
                        if len(mailto_matches) != 1:
                            msg = 'Error: {0} is an invalid DMARC URI'.format(uri)
                            handle_syntax_error('[DMARC]', domain, '{0}'.format(msg))
                            domain.valid_dmarc = False
                        else:
                            email_address = mailto_matches[0]
                            email_domain = email_address.split('@')[-1]
                            if email_domain.lower() != domain.domain_name.lower():
                                target = '{0}._report._dmarc.{1}'.format(domain.domain_name, email_domain)
                                error_message = '{0} does not indicate that it accepts DMARC reports about {1} - ' \
                                                'https://tools.ietf.org' \
                                                '/html/rfc7489#section-7.1'.format(email_domain,
                                                                                   domain.domain_name)
                                try:
                                    answer = resolver.query(target, 'TXT', tcp=True)[0].to_text().strip('"')
                                    if not answer.startswith('v=DMARC1'):
                                        handle_error('[DMARC]', domain, '{0}'.format(error_message))
                                        domain.valid_dmarc = False
                                except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                                    handle_error('[DMARC]', domain, '{0}'.format(error_message))
                                    domain.valid_dmarc = False
                                try:
                                    # Ensure ruf/rua/email domains have MX records
                                    resolver.query(email_domain, 'MX', tcp=True)
                                except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                                    handle_error('[DMARC]', domain, 'The domain for reporting '
                                                                    'address {0} does not have any '
                                                                    'MX records'.format(email_address))
                                    domain.valid_dmarc = False

    except (dns.resolver.NoNameservers, dns.resolver.NoAnswer, dns.exception.Timeout, dns.resolver.NXDOMAIN) as error:
        handle_error('[DMARC]', domain, error)


def find_host_from_ip(resolver, ip_addr):
    # Use TCP, since we care about the content and correctness of the records
    # more than whether their records fit in a single UDP packet.
    hostname, _ = resolver.query(dns.reversename.from_address(ip_addr), 'PTR', tcp=True)
    return str(hostname)


def scan(domain_name, timeout, smtp_timeout, smtp_localhost, smtp_ports, smtp_cache, scan_types, dns_hostnames):
    #
    # Configure the dnspython library
    #

    # Set some timeouts
    dns.resolver.timeout = float(timeout)
    dns.resolver.lifetime = float(timeout)

    # Our resolver
    #
    # Note that it uses the system configuration in /etc/resolv.conf
    # if no DNS hostnames are specified.
    resolver = dns.resolver.Resolver(configure=not dns_hostnames)
    # Retry queries if we receive a SERVFAIL response.  This may only indicate
    # a temporary network problem.
    resolver.retry_servfail = True
    # If the user passed in DNS hostnames to query against then use them
    if dns_hostnames:
        resolver.nameservers = dns_hostnames

    #
    # The spf library uses py3dns behind the scenes, so we need to configure
    # that too
    #
    DNS.defaults['timeout'] = timeout
    # Use TCP instead of UDP
    DNS.defaults['protocol'] = 'tcp'
    # If the user passed in DNS hostnames to query against then use them
    if dns_hostnames:
        DNS.defaults['server'] = dns_hostnames

    # Domain's constructor needs all these parameters because it does a DMARC
    # scan in its init
    domain = Domain(domain_name, timeout, smtp_timeout, smtp_localhost, smtp_ports, smtp_cache, dns_hostnames)

    logging.debug('[{0}]'.format(domain_name.lower()))

    if scan_types['mx'] and domain.is_live:
        mx_scan(resolver, domain)

    if scan_types['starttls'] and domain.is_live:
        starttls_scan(domain, smtp_timeout, smtp_localhost, smtp_ports, smtp_cache)

    if scan_types['spf'] and domain.is_live:
        spf_scan(resolver, domain)

    if scan_types['dmarc'] and domain.is_live:
        dmarc_scan(resolver, domain)

    # If the user didn't specify any scans then run a full scan.
    if domain.is_live and not (scan_types['mx'] or scan_types['starttls'] or scan_types['spf'] or scan_types['dmarc']):
        mx_scan(resolver, domain)
        starttls_scan(domain, smtp_timeout, smtp_localhost, smtp_ports, smtp_cache)
        spf_scan(resolver, domain)
        dmarc_scan(resolver, domain)

    return domain


def handle_error(prefix, domain, error, syntax_error=False):
    """Handle an error by logging via the Python logging library and
    recording it in the debug_info or syntax_error members of the
    trustymail.Domain object.

    Since the "Debug Info" and "Syntax Error" fields in the CSV output
    of trustymail come directly from the debug_info and syntax_error
    members of the trustymail.Domain object, and that CSV is likely
    all we will have to reconstruct how trustymail reached the
    conclusions it did, it is vital to record as much helpful
    information as possible.

    Parameters
    ----------
    prefix : str
        The prefix to use when constructing the log string.  This is
        usually the type of trustymail test that was being performed
        when the error condition occurred.

    domain : trustymail.Domain
        The Domain object in which the error or syntax error should be
        recorded.

    error : str, BaseException, or Exception
        Either a string describing the error, or an exception object
        representing the error.

    syntax_error : bool
        If True then the error will be recorded in the syntax_error
        member of the trustymail.Domain object.  Otherwise it is
        recorded in the error member of the trustymail.Domain object.
    """
    # Get the previous frame in the stack - the one that is calling
    # this function
    frame = inspect.currentframe().f_back
    function = frame.f_code
    function_name = function.co_name
    filename = function.co_filename
    line = frame.f_lineno

    error_template = '{prefix} In {function_name} at {filename}:{line}: {error}'

    if hasattr(error, 'message'):
        if syntax_error and 'NXDOMAIN' in error.message and prefix != '[DMARC]':
            domain.is_live = False
        error_string = error_template.format(prefix=prefix, function_name=function_name, line=line, filename=filename,
                                             error=error.message)
    else:
        error_string = error_template.format(prefix=prefix, function_name=function_name, line=line, filename=filename,
                                             error=str(error))

    if syntax_error:
        domain.syntax_errors.append(error_string)
    else:
        domain.debug_info.append(error_string)
    logging.debug(error_string)


def handle_syntax_error(prefix, domain, error):
    """Convenience method for handle_error"""
    handle_error(prefix, domain, error, syntax_error=True)


def generate_csv(domains, file_name):
    with open(file_name, 'w', encoding='utf-8', newline='\n') as output_file:
        writer = csv.DictWriter(output_file, fieldnames=domains[0].generate_results().keys())

        # First row should always be the headers
        writer.writeheader()

        for domain in domains:
            writer.writerow(domain.generate_results())
            output_file.flush()


def generate_json(domains):
    output = []
    for domain in domains:
        output.append(domain.generate_results())

    return json.dumps(output, sort_keys=True,
                      indent=2, default=format_datetime)


# Taken from pshtt to keep formatting similar
def format_datetime(obj):
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    elif isinstance(obj, str):
        return obj
    else:
        return None
