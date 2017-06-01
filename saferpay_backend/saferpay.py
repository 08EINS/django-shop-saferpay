# -*- coding: utf-8 -*-

import urlparse

import googlemaps
from django.conf.urls import patterns, url
from django.contrib.auth.tokens import default_token_generator
from django.contrib.sites.models import Site, get_current_site
from django.core.mail.message import EmailMultiAlternatives
from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import render_to_response
from django.template import loader
from django.template.context import RequestContext
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils.translation import get_language, ugettext_lazy as _
from project.settings.default import SHOP_COMPONENT, CMS_COMPONENT
from project.models.order import HeimgartnerOrder, OrderItem
from project.utils.shipping_price_calc import calc_special_shipping_cost, calc_regular_shipping_cost
from shop.models.order import BaseOrder as Order, OrderPayment
import requests

from saferpay_backend import settings
from saferpay_backend.tasks import payment_complete
import logging
import urlparse
import re

from shop.money import Money

logger = logging.getLogger('shop.payment.saferpay')


class SaferPayBackend(object):
    backend_name = _("Zahlung per Kreditkarte")
    url_namespace = "saferpay"

    backend_description = _('credit card')

    def     __init__(self, shop):
        self.shop = shop

    def round_to_5(self, amount):
        new_am = float(amount) * 10
        new_am = round(new_am * 2) / 2
        return new_am / 10

    def round_to_50(self, amount):
        new_am = float(amount) / 10
        new_am = round(new_am * 2) / 2
        return int(new_am * 10)

    def pay(self, request):
        protocol = 'https' if request.is_secure() else 'http'
        host = CMS_COMPONENT
        shop = self.shop
        order = HeimgartnerOrder.objects.get(id=request.session.pop('order'))
        # order.status = 'payment_confirmed'
        order.save()
        # domain = 'http://localhost:8000'
        domain = '%s://%s' % (protocol, host)

        order.shipping_costs = float(PriceCalculator().get_shipping_cost(order))
        mwst_shipping = self.round_to_5(order.shipping_costs * 0.08) # TODO: don't hard code this

        order.total = Money(self.round_to_5(order._total))

        if order.shipping_costs != -1.0:
            order.end_total = order.total + Money(order.shipping_costs) + Money(mwst_shipping)
        else:
            order.end_total = order.total

        order.total = order.end_total
        order._total = order.end_total
        order.mwst_new = order.mwst + mwst_shipping
        order.save()

        request.session['ORDER_ID'] = order.id

        data = {
            'AMOUNT': self.round_to_50(int(order.end_total * 100)), # TODO: DOMO: Recheck this
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
        order_id = [attribute for attribute in request.GET['DATA'].split(' ') if 'ORDERID' in attribute]

        if len(order_id) == 1:
            order_id = int(re.findall('"([^"]*)"', order_id[0])[0])
        else:
            return self.failure(request)

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

            # create additional data for email template
            order.shipping_costs = float(PriceCalculator().get_shipping_cost(order))
            mwst_shipping = self.round_to_5(order.shipping_costs * 0.08)  # TODO: don't hard code this
            
            order.total = Money(self.round_to_5(order._total))
            
            if order.shipping_costs != -1.0:
                order.end_total = order.total + Money(order.shipping_costs) + Money(mwst_shipping)
            else:
                order.end_total = order.total
            order.total = order.end_total
            order.mwst_new = order.mwst + mwst_shipping
            order.save()  # force order.modified to be bumped (we rely on this in the "thank you" view)

            self.send_confirmation_email(request, order)

            return self.success(request)
        return self.failure(request)

    def cancel(self, request):
        if request.META['HTTP_HOST'] == SHOP_COMPONENT:
            request.META['HTTP_HOST'] = CMS_COMPONENT
        return HttpResponseRedirect(reverse('order_cancelled'))

    def failure(self, request):
        order_id = [attribute for attribute in request.GET['DATA'].split(' ') if 'ORDERID' in attribute]

        if len(order_id) == 1:
            order_id = int(re.findall('"([^"]*)"', order_id[0])[0])
        else:
            return self.failure(request)

        order = HeimgartnerOrder.objects.get(id=order_id)
        if not order:
            raise Http404
        if request.META['HTTP_HOST'] == SHOP_COMPONENT:
            request.META['HTTP_HOST'] = CMS_COMPONENT
            
        return HttpResponseRedirect(reverse('order_cancelled'))

    def success(self, request):
        if request.META['HTTP_HOST'] == SHOP_COMPONENT:
            request.META['HTTP_HOST'] = CMS_COMPONENT
        return HttpResponseRedirect(reverse('thank_you_for_your_order'))

    def get_urls(self):
        return patterns('',
            url(r'^$', self.pay, name='saferpay-pay'),
            url(r'^v/$', self.verify, name='saferpay-verify'),
            url(r'^c/$', self.cancel, name='saferpay-cancel'),
            url(r'^f/$', self.failure, name='saferpay-failure'),
        )


    def send_confirmation_email(self, request, order, domain_override=None,
                          subject_template_name='email/order/order_confirmation_subject.txt',
                          email_template_name='email/order/order_confirmation_email_saferpay.html',
                          use_https=False,
                          from_email='info@heimgartner.com'):



        order_items_count = OrderItem.objects.filter(order=order).count()

        #billing_address = BaseShippingAddress.objects.filter()


        if not domain_override:
            current_site = get_current_site(request)
            site_name = current_site.name
            domain = current_site.domain
        else:
            site_name = domain = domain_override

        shipping = float(PriceCalculator().get_shipping_cost(order))
        c = {
            'email': order.email,
            'domain': domain,
            'site_name': site_name,
            'user': order.customer,
            'order': order,
            'order_items': OrderItem.objects.filter(order=order),
            'shipping_costs': shipping,
            'subtotal': order.subtotal,
            'mwst': self.round_to_5(order.mwst),
            'total': self.round_to_5((order.total + Money(shipping))),
            'protocol': use_https and 'https' or 'http',
        }
        subject = loader.render_to_string(subject_template_name, c)
        subject = ''.join(subject.splitlines())
        html_content = render_to_string(email_template_name, context_instance=RequestContext(request, c))
        text_content = strip_tags(html_content)

        msg = EmailMultiAlternatives(subject, text_content, from_email, [order.email], bcc=[from_email])
        msg.attach_alternative(html_content, "text/html")
        msg.send()


class PriceCalculator(object):

    def get_distance(self, order):
        """ Get the distance between the company and the delivery centre """
        client = googlemaps.Client('AIzaSyA9POK-qH190vWPzfuAzKID6I9hYGcPGtQ')
        shipping = ', '.join(map(str, order.shipping_address_text.split('\n')[2:]))
        origin = 'Zürcherstrasse 37, 9501 Wil/SG'

        matrix = client.distance_matrix(origin, shipping)

        try:
            kilometers = int(round(matrix['rows'][0]['elements'][0]['distance']['value']/1000))
        except:
            kilometers = int(100)
        return kilometers

    def is_bulky(self, items):
        """Check if products are bulky"""
        for item in items:
            if item.product.transport_key == 2:
                return True
        return False

    def camion(self, items):
        """ Check if delivery will be done with a Camion """
        for item in items:
            if item.product.transport_key == 3:
                return True
        return False

    def deliverable(self, items):
        """ check if a item cant be delivered """
        for item in items:
            if item.product.transport_key == 9:
                return False
        return True
    
    def envelope_shipping(self, items):
        """Check if can be shipped in envelope"""
        if len(items) > 1:
            return False
        else:
            for item in items:
                if ' x ' in item.product.dimensions:
                    try:
                        dimensions = str(item.product.dimensions).replace(' cm', '').split(' x ')
                        for dimension in dimensions:
                            if int(dimension) > 58:
                                return False
                    except Exception:
                        return False

                    return True
                else:
                    return False

    def get_shipping_cost(self, order):
        """ Calculating shipping costs by distance and type of delivery """
        order_items = OrderItem.objects.filter(order=order)
        weight = self.get_overall_weight(order_items)
        distance = self.get_distance(order)


        if self.camion(order_items):
            return calc_special_shipping_cost(weight, distance)
        elif self.is_bulky(order_items):
            return calc_regular_shipping_cost(99999999, 'A')
        elif self.envelope_shipping(order_items):
            return 6. #default envelope

        if self.deliverable(order_items):
            return calc_regular_shipping_cost(weight)
        else:
            return -1


    def get_overall_weight(self, items):
        """ Calculate the delivery weight """
        weight = 0
        for item in items:
            weight += item.product.weight * item.quantity

        return weight


