"""Selectores CSS centralizados por tienda.

Si una tienda cambia su estructura HTML, solo se actualiza este archivo.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AmazonSelectors:
    captcha_markers: tuple[str, ...] = (
        "#captchacharacters",
        "form[action*='validateCaptcha']",
    )
    title: str = "#productTitle"
    price: str = (
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen, "
        "#corePrice_feature_div .a-offscreen, "
        ".a-price.priceToPay .a-offscreen"
    )
    list_price: str = (
        "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen, "
        ".a-price[data-a-strike='true'] .a-offscreen"
    )
    seller: str = (
        "#merchantInfoFeature_feature_div .offer-display-feature-text-message, "
        "#sellerProfileTriggerId"
    )
    merchant_id_input: str = "input#merchantID, input[name='merchantID']"
    asin_input: str = "input#ASIN, input[name='ASIN']"
    availability: str = "#availability"
    delivery_block: str = "#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"
    add_to_cart: str = "#add-to-cart-button"
    glow_trigger: str = "#nav-global-location-popover-link"
    glow_zip_input: str = "#GLUXZipUpdateInput"
    glow_confirm: str = "#GLUXZipUpdate input, #GLUXZipUpdate"


@dataclass(frozen=True)
class WalmartSelectors:
    captcha_markers: tuple[str, ...] = (
        "#px-captcha",
        "iframe[src*='captcha']",
    )
    next_data: str = "script#__NEXT_DATA__"
    fulfillment_trigger: str = "[data-automation-id='fulfillment-banner']"
    postal_code_input: str = "input[name='postalCode']"
    save_postal_code: str = "[data-automation-id='save-postal-code']"


@dataclass(frozen=True)
class MercadoLibreSelectors:
    captcha_markers: tuple[str, ...] = (
        "#g-recaptcha",
        "iframe[src*='recaptcha']",
        "#captcha",
    )
    title: str = "h1.ui-pdp-title"
    price: str = ".ui-pdp-price__second-line .andes-money-amount__fraction"
    list_price: str = ".ui-pdp-price__original-value .andes-money-amount__fraction"
    official_badge: str = ".ui-pdp-official-store-label"
    seller_name: str = ".ui-pdp-seller__header__title"
    preloaded_state: str = "script#__PRELOADED_STATE__"
    cp_trigger: str = "[data-testid='action-row-cp'], a.nav-menu-cp"
    cp_input: str = "input[name='zipcode'], input[data-testid='zip-code-textfield']"
    cp_submit: str = "button[data-testid='button-use-zipcode']"
    buy_button: str = "form.ui-pdp-actions button[type='submit'], #buybox-form button[type='submit']"
    shipping_summary: str = "#shipping_summary, .ui-pdp-shipping"


@dataclass(frozen=True)
class OfficeDepotSelectors:
    captcha_markers: tuple[str, ...] = (
        "#px-captcha",
        "#g-recaptcha",
        "iframe[src*='captcha']",
    )
    title: str = "h1.pdp-product-name, h1[itemprop='name'], h1"
    price: str = ".pdp-price-current, .discountedPrice, [itemprop='price']"
    list_price: str = ".pdp-price-before, .beforePrice, s.price-before"
    seller: str = "[data-testid='seller-name'], .pdp-seller-name"
    sku: str = "[data-product-code], [itemprop='sku']"
    cp_trigger: str = "[data-testid='cp-trigger'], #open-cp-modal"
    cp_input: str = "input[name='postalCode'], #postalCodeInput"
    cp_submit: str = "[data-testid='save-cp'], #savePostalCode"
    add_to_cart: str = "#addToCartButton:not([disabled]), .btn-add-to-cart:not([disabled])"
    availability_msg: str = "#stock-status, .availability-message"


@dataclass(frozen=True)
class LaComerSelectors:
    captcha_markers: tuple[str, ...] = (
        "#px-captcha",
        "#g-recaptcha",
        "iframe[src*='captcha']",
    )
    title: str = "h1.title-product, h1.product-title, h1"
    price: str = ".price-promo, .precio-oferta, .product-price"
    list_price: str = ".price-regular, .precio-normal, s.price-before"
    seller: str = "[data-testid='seller-name'], .pdp-seller-name"
    sku: str = "[data-sku], [itemprop='sku']"
    cp_trigger: str = "[data-testid='cp-selector'], #btn-change-cp"
    cp_input: str = "input[name='postalCode'], input[name='cp'], #cpModalInput"
    cp_submit: str = "[data-testid='save-cp'], #btnSaveCp"
    add_to_cart: str = "button.btn-add-cart:not([disabled]), [data-testid='add-to-cart']:not([disabled])"
    availability_msg: str = "#availability-status, .stock-message"


@dataclass(frozen=True)
class SuburbiaSelectors:
    captcha_markers: tuple[str, ...] = (
        "#px-captcha",
        "#g-recaptcha",
        "iframe[src*='captcha']",
    )
    title: str = "h1.a-product__information--title, h1.pdp-product-name, h1"
    price: str = ".a-product__paragraphDiscountPrice, .pdp-price-current, .price-promo, [itemprop='price']"
    list_price: str = ".a-product__paragraphRegularPrice, .pdp-price-before, .price-regular, s.price-before"
    seller: str = ".a-product__infoSeller, [data-testid='seller-name'], .pdp-seller-name"
    sku: str = "[data-product-code], [data-sku], [itemprop='sku']"
    cp_trigger: str = "[data-testid='cp-trigger'], .a-header__topLink--postalCode, #btn-change-cp"
    cp_input: str = "input[name='postalCode'], input[name='cp'], #postalCodeInput"
    cp_submit: str = "[data-testid='save-cp'], #savePostalCode, #btnSaveCp"
    add_to_cart: str = "#opc_pdp_addCartButton:not([disabled]), #addToCartButton:not([disabled]), .btn-add-cart:not([disabled])"
    availability_msg: str = "#stock-status, .availability-message, .stock-message"


@dataclass(frozen=True)
class CoppelSelectors:
    captcha_markers: tuple[str, ...] = (
        "#px-captcha",
        "#g-recaptcha",
        "iframe[src*='captcha']",
    )
    title: str = "h1.pdp-title, h1.pdp-product-name, h1"
    price: str = ".pdp-price-current, .price-discounted, .price-promo, [itemprop='price']"
    list_price: str = ".pdp-price-old, .pdp-price-before, .price-regular, s.price-before"
    seller: str = ".pdp-seller-name, [data-testid='seller-info'], [data-testid='seller-name']"
    sku: str = "[data-sku], [data-product-code], [itemprop='sku']"
    cp_trigger: str = "[data-testid='cp-trigger'], [data-testid='cp-selector'], #btn-change-cp"
    cp_input: str = "input[name='postalCode'], input[name='cp'], #postalCodeInput"
    cp_submit: str = "[data-testid='save-cp'], #savePostalCode, #btnSaveCp"
    add_to_cart: str = "#addToCartButton:not([disabled]), [data-testid='add-to-cart']:not([disabled]), .btn-add-cart:not([disabled])"
    availability_msg: str = "#stock-status, .availability-message, .stock-message"


AMAZON_SELECTORS = AmazonSelectors()
WALMART_SELECTORS = WalmartSelectors()
MERCADOLIBRE_SELECTORS = MercadoLibreSelectors()
OFFICE_DEPOT_SELECTORS = OfficeDepotSelectors()
LA_COMER_SELECTORS = LaComerSelectors()
SUBURBIA_SELECTORS = SuburbiaSelectors()
COPPEL_SELECTORS = CoppelSelectors()

