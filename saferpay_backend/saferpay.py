import urlparse

from django.conf.urls import patterns, url
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import render_to_response
from django.template.context import RequestContext
from django.utils.translation import get_language, ugettext_lazy as _

from project.models.order import HeimgartnerOrder
from shop.models.order import BaseOrder as Order, OrderPayment
import requests

from saferpay_backend import settings
from saferpay_backend.tasks import payment_complete
import logging
import urlparse

logger = logging.getLogger('shop.payment.saferpay')


class SaferPayBackend(object):
    backend_name = _("Zahlung per Kreditkarte")
    url_namespace = "saferpay"

    backend_description = _('credit card')


    settings.PROCESS_URL = 'https://test.saferpay.com/hosting/CreatePayInit.asp'
    settings.VERIFY_URL = 'https://test.saferpay.com/hosting/VerifyPayConfirm.asp'
    settings.PAYMENT_COMPLETE_URL = 'https://test.saferpay.com/hosting/PayComplete.asp'

    def     __init__(self, shop):
        self.shop = shop

    def pay(self, request):
        protocol = 'https' if request.is_secure() else 'http'
        shop = self.shop
        order = HeimgartnerOrder.objects.get(id=request.session.pop('order'))
        # order.status = 'payment_confirmed'
        order.save()
        # domain = 'http://localhost:8000'
        domain = '%s://%s' % (protocol, request.META['HTTP_HOST'])

        request.session['ORDER_ID'] = order.id

        data = {
            'AMOUNT': int(order.total * 100),
            'CURRENCY': 'CHF', # TODO: don't hard code this
            'DESCRIPTION': 'Order '+str(order.number),
            'LANGID': get_language()[:2],
            'ALLOWCOLLECT': 'yes' if settings.ALLOW_COLLECT else 'no',
            'DELIVERY': 'yes' if settings.DELIVERY else 'no',
            'ACCOUNTID': settings.ACCOUNT_ID,
            'ORDERID': order.id,
            'SUCCESSLINK': domain +  reverse('saferpay-verify'),
            'BACKLINK': domain + reverse(settings.CANCEL_URL_NAME),
            'FAILLINK': domain + reverse(settings.FAILURE_URL_NAME),
            
        }
        for style in ('BODYCOLOR', 'HEADCOLOR', 'HEADLINECOLOR', 'MENUCOLOR', 'BODYFONTCOLOR', 'HEADFONTCOLOR', 'MENUFONTCOLOR', 'FONT'):
            style_value = getattr(settings, style)
            if style_value is not None:
                data[style] = style_value
        
        response = requests.get(settings.PROCESS_URL, params=data)
        logger.info('Saferpay: order %d\tredirected to saferpay gateway', order.pk)
        return HttpResponseRedirect(response.content)

    def verify(self, request):
        data = request.META['QUERY_STRING'].split("+")
        orderid_raw = [s for s in data if 'ORDERID' in s]
        order_id = int(str(orderid_raw[0]).replace('ORDERID', '').replace('%3d', '').replace('%22', ''))

        order = HeimgartnerOrder.objects.get(id=order_id)
        if not order:
            return self.failure(request)
        data = {
            'SIGNATURE': request.GET.get('SIGNATURE', ''),
            'DATA': request.GET.get('DATA', ''),
        }
        logger.info('Saferpay: order %i\tverifying , DATA: %s, SIGNATURE %s', order.pk, data['DATA'], data['SIGNATURE'])
        response = requests.get(settings.VERIFY_URL, params=data)
        if response.status_code == 200 and response.content.startswith('OK'):
            response_data = urlparse.parse_qs(response.content[3:])

            transaction_id = response_data['ID'][0]
            payment = OrderPayment(order=order, amount=order.total, transaction_id=transaction_id, payment_method='Saferpay')
            payment.save()
            order.acknowledge_payment()

            params = {'ACCOUNTID': settings.ACCOUNT_ID, 'ID': transaction_id, 'spPassword': settings.ACCOUNT_PASSWORD}
            logger.info('Saferpay: order %i\ttransaction: %s\tpayment verified', order.pk, transaction_id)
            if settings.USE_CELERY:
                payment_complete.delay(params=params, order_id=order.pk)
            else:
                try:
                    payment_complete(params=params, order_id=order.pk)
                except Exception:
                    pass # this is already logged in payment_complete
            order.save()  # force order.modified to be bumped (we rely on this in the "thank you" view)
            return self.success(request)
        return self.failure(request)

    def cancel(self, request):
        order_id = request.session.pop('ORDER_ID')

        order = HeimgartnerOrder.objects.get(id=order_id)
        if not order:
            raise Http404
        return HttpResponseRedirect(reverse('shop_welcome'))

    def failure(self, request):
        order_id = request.session.pop('ORDER_ID')

        order = HeimgartnerOrder.objects.get(id=order_id)
        if not order:
            raise Http404
        return HttpResponseRedirect(reverse('shop_welcome'))

    def success(self, request):
        return HttpResponseRedirect(reverse('thank_you_for_your_order'))

    def get_urls(self):
        return patterns('',
            url(r'^$', self.pay, name='saferpay'),
            url(r'^v/$', self.verify, name='saferpay-verify'),
            url(r'^c/$', self.cancel, name='saferpay-cancel'),
            url(r'^f/$', self.failure, name='saferpay-failure'),
        )
